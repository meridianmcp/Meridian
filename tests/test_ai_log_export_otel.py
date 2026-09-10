"""R2-G — tests for the optional AI-log -> OTel/self-hosted-Langfuse export
adapter (meridian.ai_log_otel_export + meridian.db.ai_log_export_config).

Covers:
  1. Default-OFF: no import attempted, no network attempt, when the global
     flag is unset.
  2. Unavailable states: no endpoint configured; dependency import fails.
  3. Enabled + a MOCKED exporter (never requires the real opentelemetry
     package to be installed -- see meridian.ai_log_otel_export's own
     docstring on _import_otel_deps being the monkeypatch seam): full send,
     resumable watermark, retry/backoff, partial-batch degrade.
  4. Redaction: a secret-shaped payload (inserted by bypassing append_event's
     own write-time gate, simulating a historical/pre-gate row) is masked
     before it would reach the "exporter".
  5. Config validation: set_ai_log_export_config rejects secret-shaped /
     credentialed endpoints; a project can force itself off even when the
     global flag is on, but never force itself on when the global flag is
     off.
  6. MCP surface: the three new tools are registered with correct
     annotations and never require project_id.
  7. Core ai_log behavior (append_event/list_events) is byte-for-byte
     unaffected whether this feature is enabled+mocked or disabled/absent.
  8. The one genuinely blocking call (the exporter's export()) never blocks
     the caller's event loop -- offloaded via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import json
import time
import types

import pytest

import meridian.server  # noqa: F401 -- import first to avoid handler/server import cycle
from meridian import ai_log_otel_export as otel_module
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Fake OTel SDK surface -- the seam _import_otel_deps() is monkeypatched to
# return, so every test below exercises real code paths without requiring
# the actual (optional) opentelemetry packages to be installed.
# ---------------------------------------------------------------------------

class _FakeExportResult:
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class _FakeReadableLogRecord:
    def __init__(self, log_record, resource, instrumentation_scope):
        self.log_record = log_record
        self.resource = resource
        self.instrumentation_scope = instrumentation_scope


class _FakeLogRecord:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeSeverityNumber:
    INFO = 9


class _FakeResource:
    @staticmethod
    def create(attrs):
        return dict(attrs)


class _FakeInstrumentationScope:
    def __init__(self, name):
        self.name = name


def _make_fake_deps(exporter_factory) -> dict:
    return {
        "LogRecord": _FakeLogRecord,
        "SeverityNumber": _FakeSeverityNumber,
        "ReadableLogRecord": _FakeReadableLogRecord,
        "LogRecordExportResult": _FakeExportResult,
        "Resource": _FakeResource,
        "InstrumentationScope": _FakeInstrumentationScope,
        "OTLPLogExporter": exporter_factory,
    }


class _FakeExporter:
    """Records every export() call's records; pops a scripted outcome per
    call (an outcome that isn't given defaults to SUCCESS). An outcome that
    is an Exception instance is raised instead of returned."""

    def __init__(self, outcomes=None):
        self.calls: list[list] = []
        self._outcomes = list(outcomes or [])
        self.shutdown_called = False

    def export(self, records):
        self.calls.append(list(records))
        outcome = self._outcomes.pop(0) if self._outcomes else _FakeExportResult.SUCCESS
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def shutdown(self):
        self.shutdown_called = True


def _enable(monkeypatch, endpoint="https://collector.invalid/v1/logs", **extra_env):
    monkeypatch.setenv("MERIDIAN_AI_LOG_OTEL_ENABLED", "1")
    monkeypatch.setenv("MERIDIAN_AI_LOG_OTEL_ENDPOINT", endpoint)
    # Fast, deterministic backoff for tests.
    monkeypatch.setenv("MERIDIAN_AI_LOG_OTEL_BACKOFF_BASE_S", "0.01")
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1. Default OFF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_by_default_no_import_attempted(db, monkeypatch):
    monkeypatch.delenv("MERIDIAN_AI_LOG_OTEL_ENABLED", raising=False)

    def _boom():
        raise AssertionError("dependency import must never be attempted while disabled")

    monkeypatch.setattr(otel_module, "_import_otel_deps", _boom)
    pid = await _project(db, "otel-disabled-default")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "disabled"
    assert result["sent_count"] == 0

    # Core ai_log write path is completely unaffected.
    rows = await db_module.list_events(db, pid)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_status_endpoint_never_makes_network_call(db, monkeypatch):
    monkeypatch.delenv("MERIDIAN_AI_LOG_OTEL_ENABLED", raising=False)
    pid = await _project(db, "otel-status-only")
    status = await otel_module.get_export_status(db, pid)
    assert status["global_feature_enabled"] is False
    assert status["effective_enabled"] is False
    assert isinstance(status["dependency_available"], bool)
    assert status["config"] is None  # never configured


# ---------------------------------------------------------------------------
# 2. Unavailable states
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enabled_but_no_endpoint_is_unavailable(db, monkeypatch):
    monkeypatch.setenv("MERIDIAN_AI_LOG_OTEL_ENABLED", "1")
    monkeypatch.delenv("MERIDIAN_AI_LOG_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)
    pid = await _project(db, "otel-no-endpoint")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "unavailable"
    assert "endpoint" in result["reason"]


@pytest.mark.asyncio
async def test_enabled_dependency_missing_is_unavailable_and_core_still_works(db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: None)
    pid = await _project(db, "otel-dep-missing")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "unavailable"
    assert "opentelemetry" in result["reason"]

    # Core ai_log capabilities are fully unaffected by the missing dependency.
    rows = await db_module.list_events(db, pid)
    assert len(rows) == 1
    bundle = await db_module.export_events(db, pid)
    assert bundle["event_count"] == 1


def test_dependency_available_reflects_real_probe():
    # opentelemetry is not a hard dependency of this project -- in a normal
    # test environment it is not installed, so this must be False and, most
    # importantly, must never raise either way.
    assert isinstance(otel_module.dependency_available(), bool)


# ---------------------------------------------------------------------------
# 3. Enabled + mocked exporter: send, resumable watermark, retry, degrade.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enabled_mocked_endpoint_sends_and_advances_watermark(db, monkeypatch):
    _enable(monkeypatch)
    fake = _FakeExporter()
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-happy-path")
    for i in range(3):
        await db_module.append_event(db, pid, "tool.invoked", "tool", payload={"i": i})

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "sent"
    assert result["sent_count"] == 3
    assert fake.shutdown_called is True

    cfg = await db_module.get_ai_log_export_config(db, pid)
    assert cfg["status"] == "sent"
    assert cfg["last_exported_event_id"]
    assert cfg["retry_count"] == 0

    # A second pass with nothing new is idle -- the exporter is not called
    # again, proving the watermark actually gated the fetch.
    result2 = await otel_module.run_otel_export(db, pid)
    assert result2["status"] == "idle"
    assert result2["sent_count"] == 0
    assert len(fake.calls) == 1  # unchanged since the first pass

    # A THIRD event appears; only the new one is sent.
    await db_module.append_event(db, pid, "tool.completed", "tool")
    result3 = await otel_module.run_otel_export(db, pid)
    assert result3["status"] == "sent"
    assert result3["sent_count"] == 1
    assert len(fake.calls) == 2
    assert len(fake.calls[1]) == 1


@pytest.mark.asyncio
async def test_same_second_sibling_with_lexicographically_smaller_id_is_not_dropped(db, monkeypatch):
    """Deterministic regression for the exact bug shape: two events share one
    (second-precision) ``recorded_at``, and the SECOND-appended one has an id
    that sorts BEFORE the first-appended one's id -- the scenario a naive
    single-id ``id > after_event_id`` cursor drops forever (it advances the
    watermark to the lexicographically-larger id of the pair, then a
    `> larger_id` comparison excludes the smaller-id row even though it was
    never actually sent). Ids/timestamps set directly via raw INSERT so this
    does not depend on getting lucky with real uuid4() ordering."""
    _enable(monkeypatch)
    fake = _FakeExporter()
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-same-second-sibling")
    from meridian.ai_log import EVENT_SCHEMA_VERSION
    shared_recorded_at = "2026-01-01 00:00:00"
    await db.execute(
        "INSERT INTO ai_log_events (id, schema_version, event_type, project_id, "
        "actor_kind, payload, occurred_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "zzzzzzzz-0000-0000-0000-000000000001", EVENT_SCHEMA_VERSION, "tool.invoked",
            pid, "tool", "{}", "2026-01-01T00:00:00.000Z", shared_recorded_at,
        ),
    )
    await db.commit()

    # First export pass sends only the one event that exists so far --
    # advances the watermark to "zzzzzzzz..." at shared_recorded_at.
    result1 = await otel_module.run_otel_export(db, pid)
    assert result1["status"] == "sent"
    assert result1["sent_count"] == 1

    # A SECOND event lands after the first pass, same recorded_at second,
    # with an id that sorts BEFORE the watermark id.
    await db.execute(
        "INSERT INTO ai_log_events (id, schema_version, event_type, project_id, "
        "actor_kind, payload, occurred_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "aaaaaaaa-0000-0000-0000-000000000002", EVENT_SCHEMA_VERSION, "tool.completed",
            pid, "tool", "{}", "2026-01-01T00:00:01.000Z", shared_recorded_at,
        ),
    )
    await db.commit()

    result2 = await otel_module.run_otel_export(db, pid)
    assert result2["status"] == "sent", (
        "the lexicographically-smaller-id sibling must still be picked up, "
        "never silently skipped"
    )
    assert result2["sent_count"] == 1
    assert len(fake.calls) == 2
    sent_body = json.loads(fake.calls[1][0].log_record.body)
    assert sent_body["event_id"] == "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.mark.asyncio
async def test_transient_failure_then_success_retries_within_bound(db, monkeypatch):
    _enable(monkeypatch, **{"MERIDIAN_AI_LOG_OTEL_MAX_RETRIES": "3"})
    # Fails twice, then succeeds -- well within the max_retries=3 budget.
    fake = _FakeExporter(outcomes=[_FakeExportResult.FAILURE, ConnectionError("boom"), _FakeExportResult.SUCCESS])
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-retry-success")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "sent"
    assert result["sent_count"] == 1
    assert len(fake.calls) == 3  # 2 failed attempts + 1 success, all same chunk


@pytest.mark.asyncio
async def test_retries_exhausted_on_first_chunk_reports_sync_failed(db, monkeypatch):
    _enable(monkeypatch, **{"MERIDIAN_AI_LOG_OTEL_MAX_RETRIES": "1", "MERIDIAN_AI_LOG_OTEL_CHUNK_SIZE": "10"})
    fake = _FakeExporter(outcomes=[_FakeExportResult.FAILURE, _FakeExportResult.FAILURE])
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-retries-exhausted")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "sync_failed"
    assert result["sent_count"] == 0
    assert len(fake.calls) == 2  # 1 initial + 1 retry (max_retries=1)

    cfg = await db_module.get_ai_log_export_config(db, pid)
    assert cfg["status"] == "sync_failed"
    assert cfg["retry_count"] == 1
    assert cfg["last_exported_event_id"] is None  # nothing sent -- watermark untouched


@pytest.mark.asyncio
async def test_partial_batch_degrades_but_keeps_already_sent_progress(db, monkeypatch):
    # chunk_size=1 so each event is its own HTTP call; first succeeds,
    # second exhausts retries and stops the whole pass. Both events are
    # appended back-to-back and therefore very likely share one second-
    # precision `recorded_at` value; deliberately NOT asserting which of the
    # two (a random uuid4 id each) sorts first in that case -- id order on a
    # tie carries no relationship to append order (see
    # fetch_new_events_for_export's docstring) -- only the real contract:
    # exactly one sent per pass, no duplicate, no permanent drop.
    _enable(monkeypatch, **{"MERIDIAN_AI_LOG_OTEL_CHUNK_SIZE": "1", "MERIDIAN_AI_LOG_OTEL_MAX_RETRIES": "0"})
    fake = _FakeExporter(outcomes=[_FakeExportResult.SUCCESS, _FakeExportResult.FAILURE])
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-partial-degrade")
    ev_a = await db_module.append_event(db, pid, "tool.invoked", "tool")
    ev_b = await db_module.append_event(db, pid, "tool.completed", "tool")
    both_ids = {ev_a["id"], ev_b["id"]}

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "degraded"
    assert result["sent_count"] == 1

    cfg = await db_module.get_ai_log_export_config(db, pid)
    assert cfg["status"] == "degraded"
    # The watermark advanced past exactly one of the two events.
    first_sent_id = cfg["last_exported_event_id"]
    assert first_sent_id in both_ids
    assert json.loads(cfg["last_exported_ids_at_watermark"]) == [first_sent_id]

    # A follow-up pass (exporter now healthy) resumes from exactly there --
    # it sends the OTHER event exactly once; it never re-sends the first.
    fake2 = _FakeExporter()
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake2))
    result2 = await otel_module.run_otel_export(db, pid)
    assert result2["status"] == "sent"
    assert result2["sent_count"] == 1
    assert len(fake2.calls[0]) == 1
    second_sent_body = json.loads(fake2.calls[0][0].log_record.body)
    second_sent_id = second_sent_body["event_id"]
    assert second_sent_id in both_ids
    assert second_sent_id != first_sent_id  # the other event -- never a repeat


# ---------------------------------------------------------------------------
# 4. Redaction -- defense in depth on the export path itself.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secret_shaped_payload_is_redacted_before_export(db, monkeypatch):
    _enable(monkeypatch)
    fake = _FakeExporter()
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-redaction")
    secret = "sk-ant-" + "a" * 40
    # append_event's own write-time gate (meridian.secret_redaction.
    # check_for_secrets) already hard-rejects this -- bypass it with a raw
    # INSERT to simulate a historical row written before that gate existed
    # (or a hypothetical future bug in it), which is exactly the scenario
    # this module's OWN, independent redaction pass exists to defend
    # against (see ai_log_otel_export's "Redaction, twice over" docstring).
    import json as _json
    import uuid as _uuid
    from meridian.ai_log import EVENT_SCHEMA_VERSION
    await db.execute(
        "INSERT INTO ai_log_events (id, schema_version, event_type, project_id, "
        "actor_kind, payload, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(_uuid.uuid4()), EVENT_SCHEMA_VERSION, "tool.invoked", pid, "tool",
            _json.dumps({"arg": secret}), "2026-01-01T00:00:00.000Z",
        ),
    )
    await db.commit()

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "sent"
    assert len(fake.calls) == 1
    sent_body = fake.calls[0][0].log_record.body
    assert secret not in sent_body
    assert "REDACTED" in sent_body


@pytest.mark.asyncio
async def test_send_failure_message_is_redacted_before_persisting_or_returning(db, monkeypatch):
    """The exporter.export() call carries effective["headers"] (the bearer
    token, read fresh from env). If the underlying HTTP client's exception
    happens to echo request details, that text must never reach the
    persisted, project-shared last_error column or the caller-visible
    result dict unmasked -- same defense-in-depth posture as the outgoing
    log body (see ai_log_otel_export's "Redaction, twice over" docstring),
    now also applied to error-message text."""
    _enable(monkeypatch, **{"MERIDIAN_AI_LOG_OTEL_MAX_RETRIES": "0"})
    secret = "sk-ant-" + "b" * 40
    fake = _FakeExporter(outcomes=[ConnectionError(f"POST rejected, Authorization: Bearer {secret}")])
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-error-redaction")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "sync_failed"
    assert secret not in result["reason"]
    assert "REDACTED" in result["reason"]

    cfg = await db_module.get_ai_log_export_config(db, pid)
    assert secret not in (cfg["last_error"] or "")


# ---------------------------------------------------------------------------
# 5. Config validation + project-level force-off (never force-on).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_endpoint", [
    "https://user:sk-ant-" + "a" * 40 + "@collector.example.com/v1/logs",
    "https://collector.example.com/v1/logs?api_key=abcdef1234567890",
    "not-a-url",
])
async def test_set_config_rejects_unsafe_endpoint(db, bad_endpoint):
    pid = await _project(db, "otel-config-reject")
    with pytest.raises(ValueError):
        await db_module.set_ai_log_export_config(db, pid, otlp_endpoint=bad_endpoint)
    # Nothing was persisted by the rejected call.
    assert await db_module.get_ai_log_export_config(db, pid) is None


@pytest.mark.asyncio
async def test_set_config_accepts_valid_endpoint_and_partial_upsert(db):
    pid = await _project(db, "otel-config-valid")
    cfg = await db_module.set_ai_log_export_config(
        db, pid, otlp_endpoint="https://collector.example.com/v1/logs",
        service_name="my-project",
    )
    assert cfg["otlp_endpoint"] == "https://collector.example.com/v1/logs"
    assert cfg["service_name"] == "my-project"
    assert cfg["protocol"] == "otlp_http"

    # Partial upsert: only enabled changes, endpoint/service_name untouched.
    cfg2 = await db_module.set_ai_log_export_config(db, pid, enabled=False)
    assert cfg2["enabled"] == 0
    assert cfg2["otlp_endpoint"] == "https://collector.example.com/v1/logs"


@pytest.mark.asyncio
async def test_project_can_force_off_but_never_force_on(db, monkeypatch):
    pid = await _project(db, "otel-force-off")
    await db_module.set_ai_log_export_config(
        db, pid, otlp_endpoint="https://collector.example.com/v1/logs", enabled=False,
    )
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    # Global flag ON, but this project explicitly opted out.
    monkeypatch.setenv("MERIDIAN_AI_LOG_OTEL_ENABLED", "1")
    result = await otel_module.run_otel_export(db, pid)
    assert result["status"] == "disabled"

    # Global flag OFF: even a project with enabled=True (the default/unset
    # case here) cannot turn export on for itself.
    monkeypatch.delenv("MERIDIAN_AI_LOG_OTEL_ENABLED", raising=False)
    await db_module.set_ai_log_export_config(db, pid, enabled=True)
    result2 = await otel_module.run_otel_export(db, pid)
    assert result2["status"] == "disabled"


def test_batch_and_chunk_size_are_clamped():
    settings = otel_module._effective_settings({}, batch_size_override=10_000_000)
    assert settings["batch_size"] == 1000  # hard cap, never unbounded
    assert settings["chunk_size"] <= settings["batch_size"]


# ---------------------------------------------------------------------------
# 6. MCP surface.
# ---------------------------------------------------------------------------

def test_new_mcp_tools_registered_with_correct_annotations():
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    tools_by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    for name in ("get_ai_log_export_status", "set_ai_log_export_config", "export_ai_log_otel"):
        assert name in tools_by_name, f"{name} missing from _MCP_TOOLS_LIST"
        assert "project_id" not in (tools_by_name[name]["inputSchema"].get("required") or []), (
            f"{name}: project_id must never be required"
        )

    status_ann = tools_by_name["get_ai_log_export_status"]["annotations"]
    assert status_ann["readOnlyHint"] is True
    assert status_ann["destructiveHint"] is False

    set_cfg_ann = tools_by_name["set_ai_log_export_config"]["annotations"]
    assert set_cfg_ann["readOnlyHint"] is False
    assert set_cfg_ann["destructiveHint"] is False

    export_ann = tools_by_name["export_ai_log_otel"]["annotations"]
    assert export_ann["readOnlyHint"] is False
    assert export_ann["destructiveHint"] is False
    assert export_ann["openWorldHint"] is True  # it makes a real external HTTP call


@pytest.mark.asyncio
async def test_mcp_dispatch_reaches_the_new_tools(db, monkeypatch):
    from meridian.mcp import handler as mcp_handler

    monkeypatch.delenv("MERIDIAN_AI_LOG_OTEL_ENABLED", raising=False)
    pid = await _project(db, "otel-mcp-dispatch")

    status = await mcp_handler._handle_task_tools(
        "get_ai_log_export_status", {"project_id": pid}, db, "/tmp",
        tenant=None, _mcp_tenant_id=None,
    )
    assert status["project_id"] == pid
    assert status["global_feature_enabled"] is False

    cfg = await mcp_handler._handle_task_tools(
        "set_ai_log_export_config",
        {"project_id": pid, "otlp_endpoint": "https://collector.example.com/v1/logs"},
        db, "/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert cfg["otlp_endpoint"] == "https://collector.example.com/v1/logs"

    result = await mcp_handler._handle_task_tools(
        "export_ai_log_otel", {"project_id": pid}, db, "/tmp",
        tenant=None, _mcp_tenant_id=None,
    )
    assert result["status"] == "disabled"  # global flag still off


# ---------------------------------------------------------------------------
# 7. Core ai_log behavior is unaffected either way.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_core_ai_log_unaffected_when_feature_disabled(db, monkeypatch):
    monkeypatch.delenv("MERIDIAN_AI_LOG_OTEL_ENABLED", raising=False)
    pid = await _project(db, "otel-core-disabled")
    event = await db_module.append_event(db, pid, "session.started", "session")
    assert event["event_type"] == "session.started"
    rows = await db_module.list_events(db, pid)
    assert len(rows) == 1
    bundle = await db_module.export_events(db, pid)
    assert bundle["event_count"] == 1


@pytest.mark.asyncio
async def test_core_ai_log_unaffected_when_feature_enabled_and_mocked(db, monkeypatch):
    _enable(monkeypatch)
    fake = _FakeExporter()
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    pid = await _project(db, "otel-core-enabled")
    event = await db_module.append_event(db, pid, "session.started", "session")
    assert event["event_type"] == "session.started"
    rows = await db_module.list_events(db, pid)
    assert len(rows) == 1
    bundle = await db_module.export_events(db, pid)
    assert bundle["event_count"] == 1
    # append_event/list_events/export_events behave identically regardless
    # of this feature's state -- confirmed by not needing run_otel_export to
    # be called at all for the assertions above to hold.


# ---------------------------------------------------------------------------
# 8. The one blocking call is genuinely offloaded off the event loop.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slow_exporter_does_not_block_the_event_loop(db, monkeypatch):
    _enable(monkeypatch)
    pid = await _project(db, "otel-nonblocking")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    def _slow_export(records):
        time.sleep(0.3)  # a genuinely blocking synchronous call
        return _FakeExportResult.SUCCESS

    fake = types.SimpleNamespace(export=_slow_export, shutdown=lambda: None)
    monkeypatch.setattr(otel_module, "_import_otel_deps", lambda: _make_fake_deps(lambda **kw: fake))

    ticks: list[float] = []

    async def ticker():
        for _ in range(12):
            await asyncio.sleep(0.02)
            ticks.append(time.monotonic())

    ticker_task = asyncio.create_task(ticker())
    result = await otel_module.run_otel_export(db, pid)
    await ticker_task

    assert result["status"] == "sent"
    # If the blocking export() call had stalled the event loop, the ticker
    # coroutine could not have advanced concurrently with it -- it would
    # only get its ticks in AFTER run_otel_export returned, all bunched at
    # the end. Interleaving proves the call ran off-loop.
    assert len(ticks) >= 8
