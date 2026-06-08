"""Item 39 — error alerting (5xx counter + admin notify) tests.

Covers:
- record_5xx accumulation + sliding-window expiry
- threshold + cooldown firing logic
- middleware records 5xx response status
- middleware catches unhandled exceptions, returns clean 500 envelope, fires record
- /admin/__error_test is 404 by default, becomes available behind the env flag
"""

from __future__ import annotations

import asyncio
import time
import pytest

from meridian import error_alerting as ea


# ---------------------------------------------------------------------------
# Sliding window + threshold
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_alerting():
    ea._reset_for_tests()
    yield
    ea._reset_for_tests()


@pytest.mark.asyncio
async def test_record_under_threshold_no_alert(monkeypatch):
    """Errors below the threshold don't fire the dispatch hook."""
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_WINDOW_SECS", "60")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", "60")

    fired: list[dict] = []
    ea._set_dispatch_hook(lambda payload: fired.append(payload))

    await ea.record_5xx("/x", "session:abc", 500)
    await ea.record_5xx("/x", "session:abc", 500)
    assert fired == []


@pytest.mark.asyncio
async def test_threshold_breach_fires_alert(monkeypatch):
    """Once N errors land in the window, the dispatch hook fires once."""
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_WINDOW_SECS", "60")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", "60")

    fired: list[dict] = []
    ea._set_dispatch_hook(lambda payload: fired.append(payload))

    await ea.record_5xx("/a", None, 500)
    await ea.record_5xx("/b", "session:abc", 502)
    await ea.record_5xx("/c", "session:xyz", 500)

    assert len(fired) == 1, f"expected 1 alert, got {len(fired)}"
    payload = fired[0]
    assert payload["count"] == 3
    assert payload["window_secs"] == 60
    assert payload["last_route"] == "/c"
    assert payload["last_tenant"] == "session:xyz"
    assert payload["last_status"] == 500


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(monkeypatch):
    """After firing, further errors don't re-fire until the cooldown elapses."""
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "2")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_WINDOW_SECS", "60")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", "3600")

    fired: list[dict] = []
    ea._set_dispatch_hook(lambda payload: fired.append(payload))

    await ea.record_5xx("/a", None, 500)
    await ea.record_5xx("/b", None, 500)  # threshold met, alert 1
    await ea.record_5xx("/c", None, 500)  # still over threshold, but cooldown active
    await ea.record_5xx("/d", None, 500)

    assert len(fired) == 1, f"cooldown failed: {len(fired)} alerts fired"


@pytest.mark.asyncio
async def test_window_expiry_trims_old_events(monkeypatch):
    """Events older than WINDOW_SECS are popped from the deque."""
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "100")  # never fire
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_WINDOW_SECS", "1")

    await ea.record_5xx("/a", None, 500)
    await ea.record_5xx("/b", None, 500)
    assert len(ea._5xx_events) == 2

    time.sleep(1.1)  # let the window expire
    await ea.record_5xx("/c", None, 500)
    assert len(ea._5xx_events) == 1, "window did not trim expired events"


# ---------------------------------------------------------------------------
# Middleware integration
# ---------------------------------------------------------------------------


def test_middleware_records_5xx_response(client, monkeypatch):
    """A handler that raises HTTPException(500) is recorded by the middleware."""
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "1")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_WINDOW_SECS", "60")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", "60")
    monkeypatch.setenv("MERIDIAN_ENABLE_ERROR_TEST", "1")

    fired: list[dict] = []
    ea._set_dispatch_hook(lambda payload: fired.append(payload))

    r = client.get("/admin/__error_test?kind=500")
    assert r.status_code == 500
    # The test hook is sync (returns None, not a coroutine) so record_5xx
    # calls it inline — no event-loop yield needed before asserting.
    assert len(fired) == 1, f"middleware did not record 5xx: fired={fired}"
    assert fired[0]["last_status"] == 500
    assert fired[0]["last_route"] == "/admin/__error_test"


def test_middleware_catches_unhandled_exception(client, monkeypatch, caplog):
    """An unhandled RuntimeError from a handler returns a clean 500 envelope
    AND is fed into the 5xx counter AND is logged with route context."""
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "1")
    monkeypatch.setenv("MERIDIAN_ENABLE_ERROR_TEST", "1")

    fired: list[dict] = []
    ea._set_dispatch_hook(lambda payload: fired.append(payload))

    import logging
    with caplog.at_level(logging.ERROR, logger="meridian.server"):
        r = client.get("/admin/__error_test?kind=exception")

    assert r.status_code == 500
    body = r.json()
    assert "error" in body, f"expected error envelope, got: {body}"
    assert "traceback" not in body.get("error", "").lower(), \
        "500 envelope leaked internal traceback to the client"

    assert len(fired) == 1, "unhandled exception path did not record 5xx"
    log_text = caplog.text
    assert "unhandled" in log_text.lower()
    assert "/admin/__error_test" in log_text
    assert "RuntimeError" in log_text


def test_error_test_endpoint_404_without_flag(client, monkeypatch):
    """/admin/__error_test must be 404 when MERIDIAN_ENABLE_ERROR_TEST is unset."""
    monkeypatch.delenv("MERIDIAN_ENABLE_ERROR_TEST", raising=False)
    r = client.get("/admin/__error_test")
    assert r.status_code == 404, (
        "error-test endpoint must not be reachable without the env flag — "
        "otherwise prod gets a /500-on-demand attack surface"
    )
