"""Security tests: repo hygiene, auth gates, endpoint protection."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".zip", ".sqlite", ".db", ".pyc",
    ".woff", ".woff2", ".ttf", ".eot", ".svg",
}
_SKIP_FILES = {"test_security.py", "scan_sensitive.py"}


def _tracked_text_files():
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT,
    )
    for line in result.stdout.splitlines():
        p = ROOT / line.strip()
        if p.suffix.lower() in _SKIP_EXTENSIONS:
            continue
        if p.name in _SKIP_FILES:
            continue
        if p.is_file():
            yield p


# ---------------------------------------------------------------------------
# (a) No .env file committed
# ---------------------------------------------------------------------------

def test_no_env_file_committed():
    """No .env, secrets.env, or .env.local file may be tracked by git."""
    result = subprocess.run(
        ["git", "ls-files", "--", ".env", ".env.local", "secrets.env"],
        capture_output=True, text=True, cwd=ROOT,
    )
    tracked = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    assert tracked == [], f"Sensitive env files committed to git: {tracked}"


# ---------------------------------------------------------------------------
# (b) No real API key patterns in committed files
# ---------------------------------------------------------------------------

_KEY_PATTERNS = [
    (
        "stripe_live_key",
        re.compile(r"\bsk_live_[A-Za-z0-9]{12,}\b"),
        "Stripe live secret key",
    ),
    (
        "github_pat",
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        "GitHub personal access token",
    ),
    (
        "neon_owner_password",
        # Real Neon passwords follow neondb_owner:<password>@<host>
        # Must have actual host after the password (not just a placeholder)
        re.compile(r"neondb_owner:[A-Za-z0-9_-]{8,}@ep-[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+"),
        "Neon owner password in connection string",
    ),
    (
        "google_oauth_secret",
        re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{24,}\b"),
        "Google OAuth client secret",
    ),
]


@pytest.mark.parametrize(
    "name,pattern,description",
    _KEY_PATTERNS,
    ids=[k[0] for k in _KEY_PATTERNS],
)
def test_no_api_key_pattern(name, pattern, description):
    """No real credentials matching known secret formats in committed files."""
    hits = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(text):
            rel = path.relative_to(ROOT).as_posix()
            # Skip example files and docs — they use safe placeholder forms
            if rel.endswith((".example", ".md")) or "/docs/" in rel:
                continue
            hits.append(f"{rel}: {m.group(0)[:30]}...")
    assert hits == [], f"Found {description} in committed source files:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# (c) /health returns 200 without auth
# ---------------------------------------------------------------------------

def test_health_no_auth(client):
    """/health must respond 200 with no authentication."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ok"


# ---------------------------------------------------------------------------
# (d) /dashboard redirects to /auth/login when unauthenticated in hosted mode
# ---------------------------------------------------------------------------

def test_dashboard_redirects_to_login_hosted(monkeypatch, tmp_path):
    """Unauthenticated GET /dashboard in hosted mode must redirect to /auth/login."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)

    from fastapi.testclient import TestClient
    with TestClient(server_module.app, follow_redirects=False) as c:
        resp = c.get("/dashboard")

    assert resp.status_code == 302, f"Expected 302, got {resp.status_code}"
    location = resp.headers.get("location", "")
    assert "/auth/login" in location, f"Expected redirect to /auth/login, got: {location}"


# ---------------------------------------------------------------------------
# (e) /mcp POST rejects requests without Bearer token
# ---------------------------------------------------------------------------

def test_mcp_rejects_no_bearer(client):
    """/mcp POST without Authorization header must return 401."""
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    )
    assert resp.status_code == 401
