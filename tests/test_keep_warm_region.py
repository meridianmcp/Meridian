"""4da86be7 — keep-warm region ping logic (network mocked, no real requests)."""
from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

# scripts/ is not a package -- load the module directly by path (same pattern
# as test_deploy_drift.py).
_spec = importlib.util.spec_from_file_location(
    "keep_warm_region",
    Path(__file__).resolve().parent.parent / "scripts" / "keep_warm_region.py",
)
keep_warm_region = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keep_warm_region)  # type: ignore[union-attr]


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ping_region_success(monkeypatch):
    monkeypatch.setattr(
        keep_warm_region.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(200)
    )
    reached, detail = keep_warm_region.ping_region("https://example.test/health", "ord")
    assert reached is True
    assert "200" in detail


def test_ping_region_http_error_still_counts_as_reached(monkeypatch):
    """fly-force-region does not fall back -- ANY HTTP response (even an
    app-level error status) proves the region was actually reached."""
    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            "https://example.test/health", 503, "Service Unavailable", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(keep_warm_region.urllib.request, "urlopen", _raise)
    reached, detail = keep_warm_region.ping_region("https://example.test/health", "ord")
    assert reached is True
    assert "503" in detail
    assert "region reached" in detail


def test_ping_region_unreachable_on_timeout(monkeypatch):
    def _raise(*a, **k):
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(keep_warm_region.urllib.request, "urlopen", _raise)
    reached, detail = keep_warm_region.ping_region("https://example.test/health", "ord")
    assert reached is False
    assert "timed out" in detail


def test_main_exit_code_reflects_reachability(monkeypatch, capsys):
    monkeypatch.setattr(
        keep_warm_region, "ping_region", lambda url, region, timeout: (True, "HTTP 200")
    )
    monkeypatch.setattr("sys.argv", ["keep_warm_region.py"])
    assert keep_warm_region.main() == 0

    monkeypatch.setattr(
        keep_warm_region, "ping_region", lambda url, region, timeout: (False, "timed out")
    )
    assert keep_warm_region.main() == 1
    assert "UNREACHABLE" in capsys.readouterr().err
