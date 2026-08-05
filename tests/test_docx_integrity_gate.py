"""Tests for sprint item b67ec6b5 — GET /projects/{id}/document-review, the
non-mutating DOCX review endpoint backing the dashboard's DOCX review panel.

The real finding-producing logic (docs_intel.build_document_review) lives in
the extensions/meridian-docs package, which is NOT a core pypi-dependency and
is not installed in this pixi env (mirrors meridian.docx_integrity_gate's own
tests — see that module's docstring). So this file covers:

1. The real degrade path: the extension genuinely isn't importable here, so
   the endpoint must return a structured {"error": ...} explaining that,
   never a 500 or a crash.
2. Every other response shape (ok/empty, ok/findings, stale, file-not-found,
   builder exception) via an INJECTED stub builder — the same
   dependency-injection pattern docx_integrity_gate.build_docx_integrity_gate
   uses for its render_checker/equation_auditor/snapshot_reader params —
   monkeypatching meridian.routes.notes._resolve_document_review_builder so
   no real .docx parsing or extension install is required.
3. Ordinary route contract: unknown project -> 404, missing path -> inline
   error (not a 500), matching document_structure_endpoint's own established
   behavior in this same file.
"""
from __future__ import annotations

import sys

from meridian.routes import notes as notes_module


# ---------------------------------------------------------------------------
# _resolve_document_review_builder — real degrade path.
#
# meridian_docs is not a pixi pypi-dependency of the core package (see module
# docstring), so a bare `import meridian_docs` legitimately fails in this
# env — EXCEPT that tests/test_meridian_docs_equations.py inserts the
# extension's directory onto sys.path (module scope, so the effect persists
# for the rest of that pytest-xdist worker's process) to test the extension
# directly. Whether that import has already happened is therefore
# WORKER-ORDER-DEPENDENT under `-n auto`/`-n 3` — asserting on the ambient
# state directly would be flaky. Force the deterministic "not installed"
# state instead (the same `sys.modules[name] = None` trick Python's own
# import system treats as "known unimportable" — see PEP 328 / CPython
# importlib._bootstrap), matching docx_integrity_gate's tests never
# asserting on ambient extension-availability state either.
# ---------------------------------------------------------------------------

def test_resolve_document_review_builder_returns_none_when_extension_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "meridian_docs", None)
    assert notes_module._resolve_document_review_builder() is None


# ---------------------------------------------------------------------------
# GET /projects/{id}/document-review — ordinary route contract.
# ---------------------------------------------------------------------------

def test_document_review_endpoint_unknown_project_404(client):
    r = client.get("/projects/does-not-exist/document-review", params={"path": "x.docx"})
    assert r.status_code == 404


def test_document_review_endpoint_missing_path_is_inline_error(client):
    pid = client.post("/projects", json={"name": "docx-review-missing-path"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": ""})
    assert r.status_code == 200, r.text
    assert r.json() == {"error": "path is required"}


def test_document_review_endpoint_extension_not_installed_is_inline_error(client, monkeypatch):
    # Forces the deterministic "not installed" state (see the comment above
    # test_resolve_document_review_builder_returns_none_when_extension_absent
    # for why the ambient sys.path state can't be relied on directly under
    # xdist). Never a 500; the message names the extension so a self-hoster
    # knows how to fix it.
    monkeypatch.setitem(sys.modules, "meridian_docs", None)
    pid = client.post("/projects", json={"name": "docx-review-no-extension"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": "/tmp/report.docx"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" in body
    assert "meridian-docs" in body["error"]


# ---------------------------------------------------------------------------
# Injected-builder paths — every response shape the real
# docs_intel.build_document_review can return.
# ---------------------------------------------------------------------------

def _install_stub_builder(monkeypatch, fn):
    monkeypatch.setattr(notes_module, "_resolve_document_review_builder", lambda: fn)


def test_document_review_endpoint_ok_empty_findings(client, monkeypatch):
    calls = []

    def _stub(docx_path, *, expected_source_fingerprint=None):
        calls.append((docx_path, expected_source_fingerprint))
        return {
            "status": "ok",
            "docx_path": docx_path,
            "source_fingerprint": "abc123",
            "findings": [],
            "finding_count": 0,
            "findings_by_category": {
                "structure": 0, "equation": 0, "caption": 0, "section_page": 0,
                "ownership": 0, "provenance": 0, "render_integrity": 0,
            },
            "findings_by_severity": {},
            "categories": [
                "structure", "equation", "caption", "section_page",
                "ownership", "provenance", "render_integrity",
            ],
        }

    _install_stub_builder(monkeypatch, _stub)
    pid = client.post("/projects", json={"name": "docx-review-empty"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": "report.docx"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["finding_count"] == 0
    assert body["findings"] == []
    assert set(body["categories"]) == {
        "structure", "equation", "caption", "section_page",
        "ownership", "provenance", "render_integrity",
    }
    # path forwarded verbatim, no expected_source_fingerprint by default.
    assert calls == [("report.docx", None)]


def test_document_review_endpoint_ok_with_mixed_findings_and_locators(client, monkeypatch):
    def _stub(docx_path, *, expected_source_fingerprint=None):
        return {
            "status": "ok",
            "docx_path": docx_path,
            "source_fingerprint": "fp1",
            "findings": [
                {
                    "category": "caption", "severity": "warning",
                    "type": "legacy_plaintext_caption",
                    "detail": {"kind": "Figure", "old_cached_number": "3"},
                    "locator": {
                        "status": "resolved", "section_path": "2.1",
                        "target_para_id": "p10", "document_order": 10,
                        "quoted_text": "Figure 3. A plain caption.",
                        "leading_text_preview": "Figure 3. A plain caption.",
                        "first_words": "Figure 3. A plain caption.",
                        "word_search_locator": "Figure 3. A plain caption.",
                        "bookmark_exists": False, "candidates": [],
                    },
                },
                {
                    "category": "equation", "severity": "error",
                    "type": "duplicate_equation_number",
                    "detail": {"number": "(1)", "para_ids": ["p20", "p21"]},
                    "locator": {
                        "status": "ambiguous", "reason": "2 elements matched",
                        "candidates": [
                            {"target_para_id": "p20", "section_path": "3"},
                            {"target_para_id": "p21", "section_path": "3"},
                        ],
                    },
                },
                {
                    "category": "provenance", "severity": "info",
                    "type": "stale_note",
                    "detail": {"para_id": "p30", "text": "[NOTE: TODO]"},
                    "locator": {"status": "not_found", "reason": "para_id 'p30' not found"},
                },
            ],
            "finding_count": 3,
            "findings_by_category": {"caption": 1, "equation": 1, "provenance": 1},
            "findings_by_severity": {"warning": 1, "error": 1, "info": 1},
            "categories": [
                "structure", "equation", "caption", "section_page",
                "ownership", "provenance", "render_integrity",
            ],
        }

    _install_stub_builder(monkeypatch, _stub)
    pid = client.post("/projects", json={"name": "docx-review-findings"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": "thesis.docx"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["finding_count"] == 3
    kinds = {f["category"]: f["locator"]["status"] for f in body["findings"]}
    assert kinds == {"caption": "resolved", "equation": "ambiguous", "provenance": "not_found"}


def test_document_review_endpoint_stale(client, monkeypatch):
    def _stub(docx_path, *, expected_source_fingerprint=None):
        assert expected_source_fingerprint == "old-fp"
        return {
            "status": "stale",
            "docx_path": docx_path,
            "reason": "source_fingerprint_mismatch",
            "expected_source_fingerprint": expected_source_fingerprint,
            "source_fingerprint": "new-fp",
            "findings": [],
            "finding_count": 0,
            "findings_by_category": {},
            "findings_by_severity": {},
            "categories": [],
        }

    _install_stub_builder(monkeypatch, _stub)
    pid = client.post("/projects", json={"name": "docx-review-stale"}).json()["id"]
    r = client.get(
        f"/projects/{pid}/document-review",
        params={"path": "report.docx", "expected_source_fingerprint": "old-fp"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "stale"
    assert body["findings"] == []
    assert body["source_fingerprint"] == "new-fp"


def test_document_review_endpoint_file_not_found(client, monkeypatch):
    def _stub(docx_path, *, expected_source_fingerprint=None):
        raise FileNotFoundError(f"[Errno 2] No such file or directory: '{docx_path}'")

    _install_stub_builder(monkeypatch, _stub)
    pid = client.post("/projects", json={"name": "docx-review-missing-file"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": "nope.docx"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" in body
    assert "nope.docx" in body["error"]


def test_document_review_endpoint_builder_exception_never_500s(client, monkeypatch):
    def _stub(docx_path, *, expected_source_fingerprint=None):
        raise ValueError("could not parse: bad zip file")

    _install_stub_builder(monkeypatch, _stub)
    pid = client.post("/projects", json={"name": "docx-review-parse-error"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": "corrupt.docx"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" in body
    assert "bad zip file" in body["error"]


def test_document_review_endpoint_unexpected_return_shape_is_reported_not_trusted(client, monkeypatch):
    _install_stub_builder(monkeypatch, lambda docx_path, **kw: "not a dict")
    pid = client.post("/projects", json={"name": "docx-review-bad-shape"}).json()["id"]
    r = client.get(f"/projects/{pid}/document-review", params={"path": "report.docx"})
    assert r.status_code == 200, r.text
    assert "error" in r.json()
