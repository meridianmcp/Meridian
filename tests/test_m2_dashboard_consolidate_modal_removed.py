"""The hosted dashboard must not ask the browser for an LLM provider API key.

Provider credentials stay on the desktop. The AI decision-consolidation modal
typed a provider key into a browser password field and POSTed it to
``/projects/{id}/decisions/consolidate``. That modal and its two trigger
buttons (the Decisions-subtab "Consolidate" and the constitution panel's
"Consolidate now") are removed from ``dashboard.ts``.

The server route is a bring-your-own-key API: it rejects a request that carries
no ``api_key`` and has no server-side provider key, so it cannot run from the
dashboard without a browser-typed key. The route stays in place for API
clients, and its own tests live in ``tests/test_cov_misc.py``.

These tests pin the removal in the shipped bundle (the file the hosted image
serves) and in the TypeScript sources, and check that the manifest hash was
regenerated from the bundle that is actually shipped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STATIC = _REPO / "meridian" / "static"
_BUNDLE = _STATIC / "dashboard.bundle.js"
_MANIFEST = _STATIC / "asset-manifest.json"

# Strings unique to the removed modal, its trigger buttons, and its POST.
_CONSOLIDATE_MODAL_MARKERS = (
    "AI Decision Consolidation",
    "_consolidate-key",
    "_consolidate-model",
    "_consolidate-run",
    "_consolidate-cancel",
    "API key required",
    "consolidate-decisions-",
    "constitution-consolidate-",
    "consolidateDecisions",
    "Consolidate now",
    "/decisions/consolidate",
)


def _shipped_bundle_text() -> str:
    return _BUNDLE.read_text(encoding="utf-8")


def _dashboard_sources() -> dict[str, str]:
    sources = {"dashboard.ts": (_STATIC / "dashboard.ts").read_text(encoding="utf-8")}
    for mod in sorted(_STATIC.glob("dashboard-*.ts")):
        sources[mod.name] = mod.read_text(encoding="utf-8")
    return sources


def test_shipped_bundle_has_no_provider_key_modal_or_post():
    """The bundle the hosted image serves carries no consolidate modal, trigger
    button, or POST to /decisions/consolidate."""
    bundle = _shipped_bundle_text()
    leaked = [m for m in _CONSOLIDATE_MODAL_MARKERS if m in bundle]
    assert not leaked, f"dashboard.bundle.js still ships consolidate UI: {leaked}"


def test_dashboard_sources_have_no_provider_key_modal_or_post():
    """The TypeScript sources the bundle is built from carry no consolidate
    modal, trigger button, or POST, so a rebuild cannot bring them back."""
    for name, text in _dashboard_sources().items():
        leaked = [m for m in _CONSOLIDATE_MODAL_MARKERS if m in text]
        assert not leaked, f"{name} still contains consolidate UI: {leaked}"


def test_asset_manifest_hash_matches_shipped_bundle():
    """The manifest was regenerated from the bundle that is now on disk, so the
    browser cache-busting token points at the bundle without the modal."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    actual = hashlib.sha256(_BUNDLE.read_bytes()).hexdigest()[:12]
    assert manifest["bundle"] == "dashboard.bundle.js"
    assert manifest["bundle_hash"] == actual
