"""d188b408 — testing-gap coverage: k6 load-script guard + the OAuth
refresh-token rotation/replay chain (the security-critical OAuth path).

The Playwright user-flow E2E already lives in tests/test_demo_ux.py (CI-wired);
this file fills the remaining gaps flagged by the item.
"""
from __future__ import annotations

import asyncio
import pathlib


def _hosted_client(monkeypatch, tmp_path):
    """A hosted-mode TestClient over a fresh in-memory DB."""
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    from fastapi.testclient import TestClient
    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")
    return TestClient(server_module.app)


# ---------------------------------------------------------------------------
# k6 load-script guard — keeps the committed load artifacts from rotting
# ---------------------------------------------------------------------------

def test_k6_load_scripts_present_and_structured():
    root = pathlib.Path(__file__).resolve().parent.parent / "k6"
    for name in ("signup_smoke.js", "mcp_tools.js"):
        p = root / name
        assert p.exists(), f"missing k6 script {name}"
        text = p.read_text(encoding="utf-8")
        assert "import http from 'k6/http'" in text
        assert "export const options" in text
        assert "export default function" in text
        assert "check(" in text


# ---------------------------------------------------------------------------
# OAuth refresh-token rotation + replay rejection (in-process, full endpoint)
# ---------------------------------------------------------------------------

def test_oauth_refresh_token_rotation_and_replay(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as c:
        from meridian.routes.oauth import _issue_refresh_token

        db = c.app.state.db
        rt = asyncio.run(
            _issue_refresh_token(db, tenant_id=None, client_id="load-client")
        )

        # First exchange succeeds and returns a fresh access token.
        r1 = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": rt},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json().get("access_token", "").startswith("sk_meridian_")

        # Replaying the same refresh token is rejected (one-time use / rotation).
        r2 = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": rt},
        )
        assert r2.status_code == 400
        assert r2.json().get("error") == "invalid_grant"


def test_oauth_token_rejects_unknown_refresh_token(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as c:
        r = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "rt_meridian_nope"},
        )
        assert r.status_code == 400
        assert r.json().get("error") == "invalid_grant"
