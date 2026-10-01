"""9c1a3fd2 — GET /projects/{id}/document-review?journal=... wiring and the
new GET /journal-style-presets catalog route.

Closes the "verified journal facts never reach an enforcement path" gap
(workspace proposal cb7bd76e / sprint item b3fa6019) on the ACTUAL dashboard
surface: build_document_review already threaded style_policy through to
audit_equation_style, but never to audit_caption_style (built the same
session as this fix), and the HTTP route never exposed style_policy at all
-- a caller had no way to ask the real, live "Review findings" panel
(dashboard-documents.ts) to check a document against a journal's rules.

The meridian-docs extension is an optional, independently-installable
package (see notes.py's own docstrings), so these tests monkeypatch
meridian.routes.notes._resolve_document_review_builder /
_resolve_journal_style_preset_getter directly rather than depending on the
extension being importable from the core test environment -- the same
injectable-dependency pattern test_document_structure_endpoint's neighbors
already use for docx_integrity_gate.
"""
from __future__ import annotations

import os

from meridian.routes import notes as notes_routes

# Recorders for the user_presets_path the route actually passes into
# get_journal_style_preset / list_journal_style_presets -- without these the
# fakes below would silently accept the pre-9c1a3fd2 call shape (no
# user_presets_path at all) via their own default, and the route wiring
# could regress with no test failing. Cleared at the top of each test that
# reads them.
_style_policy_calls: list[str | None] = []
_list_presets_calls: list[str | None] = []


def _fake_style_policy(journal: str, user_presets_path: str | None = None) -> dict:
    _style_policy_calls.append(user_presets_path)
    if journal.lower() != "jcshm":
        raise ValueError(f"unknown journal style preset {journal!r}; known presets: ['jcshm']")
    return {"figure_caption_bold": True, "figure_caption_label_punctuation": "none"}


def _fake_build_document_review(docx_path, *, expected_source_fingerprint=None, style_policy=None):
    findings = []
    if style_policy is not None:
        findings.append({
            "category": "caption", "severity": "warning",
            "type": "caption_label_not_bold", "detail": {"kind": "figure"},
            "locator": {"status": "not_applicable", "candidates": []},
        })
    return {
        "status": "ok", "docx_path": docx_path, "source_fingerprint": "abc123",
        "findings": findings, "finding_count": len(findings),
        "findings_by_category": {"caption": len(findings)},
        "findings_by_severity": {"warning": len(findings)} if findings else {},
        "categories": ["structure", "equation", "caption", "section_page",
                        "ownership", "provenance", "render_integrity"],
    }


def _fake_list_presets(user_presets_path: str | None = None) -> dict:
    _list_presets_calls.append(user_presets_path)
    return {
        "presets": [{"name": "jcshm", "source": "built_in", "shadows_builtin": False}],
        "builtin_count": 1, "user_count": 0,
    }


def test_document_review_journal_param_reaches_style_policy(client, monkeypatch, tmp_path):
    """Passing ?journal=jcshm resolves a style_policy and threads it into
    build_document_review; omitting it keeps the pre-9c1a3fd2 shape (no
    style_policy, so the fake builder above returns zero findings). Also
    confirms the route's own workspace user_presets_path (not just the
    journal name) actually reaches the preset getter -- see finding 1 of
    9c1a3fd2's review."""
    _style_policy_calls.clear()
    monkeypatch.setattr(notes_routes, "_resolve_document_review_builder", lambda: _fake_build_document_review)
    monkeypatch.setattr(
        notes_routes, "_resolve_journal_style_preset_getter",
        lambda: (_fake_style_policy, _fake_list_presets),
    )
    expected_presets_path = os.path.join(str(tmp_path), "journal_style_presets.json")
    docx_path = tmp_path / "ms.docx"
    docx_path.write_bytes(b"not a real docx -- fake builder never opens it")
    pid = client.post("/projects", json={"name": "journal-review"}).json()["id"]

    # No journal param -- unchanged behavior, zero findings.
    r0 = client.get(f"/projects/{pid}/document-review", params={"path": str(docx_path)})
    assert r0.status_code == 200, r0.text
    assert r0.json()["finding_count"] == 0

    # journal=jcshm -- style_policy reaches the builder, findings appear.
    r1 = client.get(
        f"/projects/{pid}/document-review",
        params={"path": str(docx_path), "journal": "jcshm"},
    )
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["finding_count"] == 1
    assert body["findings"][0]["type"] == "caption_label_not_bold"
    assert _style_policy_calls[-1] == expected_presets_path

    # Unknown journal -- structured inline error, never a 500.
    r2 = client.get(
        f"/projects/{pid}/document-review",
        params={"path": str(docx_path), "journal": "not-a-real-journal"},
    )
    assert r2.status_code == 200
    assert "error" in r2.json()
    assert "not-a-real-journal" in r2.json()["error"]
    assert _style_policy_calls[-1] == expected_presets_path


def test_document_review_journal_without_extension_installed(client, monkeypatch, tmp_path):
    """meridian-docs not installed: document-review still degrades to an
    inline error (pre-existing behavior) even when journal= is passed."""
    monkeypatch.setattr(notes_routes, "_resolve_document_review_builder", lambda: None)
    docx_path = tmp_path / "ms.docx"
    docx_path.write_bytes(b"irrelevant")
    pid = client.post("/projects", json={"name": "no-extension"}).json()["id"]
    r = client.get(
        f"/projects/{pid}/document-review",
        params={"path": str(docx_path), "journal": "jcshm"},
    )
    assert r.status_code == 200
    assert "error" in r.json()
    assert "not installed" in r.json()["error"]


def test_document_review_journal_presets_unavailable(client, monkeypatch, tmp_path):
    """meridian-docs IS installed (builder resolves) but
    _resolve_journal_style_preset_getter() returns (None, None) -- e.g. an
    older/partial install missing get_journal_style_preset. Distinct from
    test_document_review_journal_without_extension_installed above, which
    monkeypatches the builder itself to None and so never reaches this
    branch at all; see finding 2 of 9c1a3fd2's review."""
    monkeypatch.setattr(notes_routes, "_resolve_document_review_builder", lambda: _fake_build_document_review)
    monkeypatch.setattr(notes_routes, "_resolve_journal_style_preset_getter", lambda: (None, None))
    docx_path = tmp_path / "ms.docx"
    docx_path.write_bytes(b"not a real docx -- fake builder never opens it")
    pid = client.post("/projects", json={"name": "presets-unavailable"}).json()["id"]
    r = client.get(
        f"/projects/{pid}/document-review",
        params={"path": str(docx_path), "journal": "jcshm"},
    )
    assert r.status_code == 200
    assert "error" in r.json()
    assert "journal style presets are unavailable" in r.json()["error"]


def test_journal_style_presets_catalog_endpoint(client, monkeypatch, tmp_path):
    """GET /journal-style-presets powers the picker dropdown: built-in +
    user presets, each tagged with source/shadows_builtin. Also confirms
    the route's own workspace user_presets_path reaches the lister, not
    just an unused default -- see finding 1 of 9c1a3fd2's review."""
    _list_presets_calls.clear()
    monkeypatch.setattr(
        notes_routes, "_resolve_journal_style_preset_getter",
        lambda: (_fake_style_policy, _fake_list_presets),
    )
    r = client.get("/journal-style-presets")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["builtin_count"] == 1
    assert body["presets"][0]["name"] == "jcshm"
    assert _list_presets_calls[-1] == os.path.join(str(tmp_path), "journal_style_presets.json")


def test_journal_style_presets_catalog_without_extension_installed(client, monkeypatch):
    monkeypatch.setattr(notes_routes, "_resolve_journal_style_preset_getter", lambda: (None, None))
    r = client.get("/journal-style-presets")
    assert r.status_code == 200
    assert "error" in r.json()
    assert "not installed" in r.json()["error"]
