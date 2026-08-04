"""Tests for render_gate.py -- render-capability detection (93cd9798).

render_gate is capability DETECTION, not a rendering engine (mirrors the
.pdf boundary in local_ingest.extract_text, which also declines to embed a
rendering pipeline and instead raises a concrete, actionable reason). These
tests lock in the three-state contract the sprint item requires:

  1. capability available  -> "rendered"
  2. capability missing    -> "unavailable-with-reason" (a real, itemized
     reason string -- never a generic placeholder)
  3. render attempt errors -> "failed" (never silently reported as rendered
     or as unavailable)

Backends are injected via check_render_capability's `backends=` parameter so
none of these tests depend on LibreOffice / Word actually being installed on
whatever machine runs the suite -- exactly the kind of environment gap this
module exists to report honestly rather than assume away.
"""
from __future__ import annotations

import hashlib
import zipfile
from typing import Any, Callable

from meridian_docs import render_gate, server


def _write_dummy_docx(tmp_path, name: str = "doc.docx") -> str:
    # render_gate does not parse the file itself (that's the backend's job,
    # and our fake backends below don't touch file content) -- only its
    # existence/type is checked before a backend is even consulted.
    path = tmp_path / name
    path.write_bytes(b"not a real docx -- render_gate never opens this itself")
    return str(path)


_DOCUMENT_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
{paragraphs}
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _write_real_docx(
    tmp_path,
    name: str = "doc.docx",
    *,
    paragraph_count: int = 2,
    media_files: list[str] | None = None,
    table_count: int = 0,
) -> str:
    """A genuinely valid, parseable .docx -- unlike ``_write_dummy_docx``,
    used for ``verify_promotion_readiness`` tests, which DOES parse content
    (structural counts) rather than delegating entirely to a render backend.
    """
    paragraphs = "\n".join(
        f'    <w:p w14:paraId="P{i:07d}"><w:r><w:t>Paragraph {i}.</w:t></w:r></w:p>'
        for i in range(paragraph_count)
    )
    tables = "\n".join(
        "    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        for _ in range(table_count)
    )
    document_xml = _DOCUMENT_XML_TEMPLATE.format(paragraphs=paragraphs + "\n" + tables)
    path = tmp_path / name
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
        for media_name in media_files or []:
            archive.writestr(f"word/media/{media_name}", b"fake-binary-media")
    return str(path)


def _sha256_of(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _fake_backend(
    name: str,
    *,
    available: bool,
    reason: str | None = None,
    render: Callable[[str], dict[str, Any]] | None = None,
) -> render_gate.RenderBackend:
    def _unavailable_reason() -> str | None:
        return None if available else reason

    return render_gate.RenderBackend(
        name=name,
        unavailable_reason=_unavailable_reason,
        render=render if render is not None else (lambda path: {}),
    )


# ---------------------------------------------------------------------------
# 1. capability available -> "rendered"
# ---------------------------------------------------------------------------

def test_available_backend_renders_successfully(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    backend = _fake_backend(
        "fake-ok",
        available=True,
        render=lambda path: {"converted_via": "fake-ok", "path_seen": path},
    )

    result = render_gate.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == render_gate.RENDERED
    assert result["status"] == "rendered"
    assert result["backend"] == "fake-ok"
    assert result["detail"]["path_seen"] == docx_path
    # "rendered" is the ONLY status meaning "verified" -- guard against it
    # ever doubling as the other two.
    assert result["status"] != render_gate.UNAVAILABLE_WITH_REASON
    assert result["status"] != render_gate.FAILED


def test_first_available_backend_in_the_chain_wins(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    unavailable = _fake_backend("skipped", available=False, reason="not installed")
    winner = _fake_backend("winner", available=True, render=lambda path: {"which": "winner"})
    never_reached = _fake_backend(
        "never-reached",
        available=True,
        render=lambda path: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = render_gate.check_render_capability(
        docx_path, backends=[unavailable, winner, never_reached]
    )

    assert result["status"] == render_gate.RENDERED
    assert result["backend"] == "winner"
    assert result["detail"]["which"] == "winner"


# ---------------------------------------------------------------------------
# 2. capability missing -> "unavailable-with-reason" (real, itemized reason)
# ---------------------------------------------------------------------------

def test_no_backend_available_reports_itemized_real_reason(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    backend_a = _fake_backend("backend-a", available=False, reason="binary-a not on PATH")
    backend_b = _fake_backend("backend-b", available=False, reason="binary-b not installed")

    result = render_gate.check_render_capability(docx_path, backends=[backend_a, backend_b])

    assert result["status"] == render_gate.UNAVAILABLE_WITH_REASON
    assert result["status"] == "unavailable-with-reason"
    assert "reason" in result
    reason = result["reason"]
    # A real, itemized explanation -- not a generic "unavailable" placeholder.
    assert reason.strip().lower() != "unavailable"
    assert reason.strip().lower() != "unavailable-with-reason"
    assert "backend-a" in reason and "binary-a not on PATH" in reason
    assert "backend-b" in reason and "binary-b not installed" in reason
    assert "backend" not in result  # no backend was actually selected
    assert result["status"] != render_gate.RENDERED
    assert result["status"] != render_gate.FAILED


def test_no_backends_registered_at_all_is_still_a_real_reason(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)

    result = render_gate.check_render_capability(docx_path, backends=[])

    assert result["status"] == render_gate.UNAVAILABLE_WITH_REASON
    assert "no render backend" in result["reason"].lower()


# ---------------------------------------------------------------------------
# 3. render attempt errors -> "failed" (never silently rendered/available)
# ---------------------------------------------------------------------------

def test_render_attempt_raises_capability_error_is_failed_not_rendered(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)

    def _boom(path: str) -> dict[str, Any]:
        raise render_gate.RenderCapabilityError("soffice exited with code 1: corrupt zip")

    backend = _fake_backend("fake-broken", available=True, render=_boom)

    result = render_gate.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == render_gate.FAILED
    assert result["status"] == "failed"
    assert "corrupt zip" in result["reason"]
    assert result["backend"] == "fake-broken"
    # The critical invariant this whole module exists for: a real error must
    # NEVER be reported as success, and must NEVER be folded into "we simply
    # couldn't check" -- the two mean different things to a caller.
    assert result["status"] != render_gate.RENDERED
    assert result["status"] != render_gate.UNAVAILABLE_WITH_REASON


def test_unexpected_exception_from_backend_is_also_failed(tmp_path):
    """A backend can raise something other than RenderCapabilityError (e.g. a
    raw pywintypes.com_error out of Word COM, or an OSError from subprocess) --
    check_render_capability must still classify it as 'failed', never let it
    propagate uncaught and never count it as success."""
    docx_path = _write_dummy_docx(tmp_path)

    def _explode(path: str) -> dict[str, Any]:
        raise ValueError("totally unexpected backend bug")

    backend = _fake_backend("fake-buggy", available=True, render=_explode)

    result = render_gate.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == render_gate.FAILED
    assert "totally unexpected backend bug" in result["reason"]
    assert result["backend"] == "fake-buggy"


def test_missing_file_is_failed_not_unavailable(tmp_path):
    """A bad docx_path is a concrete, checkable error about THIS call --
    distinct from 'unavailable-with-reason', which is reserved for
    environment-level capability gaps true regardless of which document was
    passed in."""
    missing_path = str(tmp_path / "does_not_exist.docx")
    backend = _fake_backend("fake-ok", available=True, render=lambda path: {})

    result = render_gate.check_render_capability(missing_path, backends=[backend])

    assert result["status"] == render_gate.FAILED
    assert "no such file" in result["reason"]


# ---------------------------------------------------------------------------
# detect_backend -- the itemization helper the "unavailable" path relies on.
# ---------------------------------------------------------------------------

def test_detect_backend_returns_first_available_and_collected_reasons():
    unavailable = _fake_backend("unavailable-one", available=False, reason="missing binary")
    available = _fake_backend("available-one", available=True)

    backend, reasons = render_gate.detect_backend([unavailable, available])

    assert backend is not None
    assert backend.name == "available-one"
    assert any("unavailable-one" in r and "missing binary" in r for r in reasons)


def test_detect_backend_none_available():
    unavailable = _fake_backend("only-one", available=False, reason="not installed")

    backend, reasons = render_gate.detect_backend([unavailable])

    assert backend is None
    assert reasons == ["only-one: not installed"]


def test_render_statuses_constant_matches_the_three_state_contract():
    assert render_gate.RENDER_STATUSES == (
        render_gate.RENDERED,
        render_gate.UNAVAILABLE_WITH_REASON,
        render_gate.FAILED,
    )
    assert render_gate.RENDERED == "rendered"
    assert render_gate.UNAVAILABLE_WITH_REASON == "unavailable-with-reason"
    assert render_gate.FAILED == "failed"


# ---------------------------------------------------------------------------
# Default (real) backend detection -- unit-tested in isolation via
# monkeypatch so it doesn't depend on the test machine's actual toolchain.
# ---------------------------------------------------------------------------

def test_default_backends_are_registered_and_named():
    names = [backend.name for backend in render_gate.KNOWN_BACKENDS]
    assert "libreoffice-soffice" in names
    assert "word-com" in names


def test_soffice_backend_unavailable_reason_when_not_on_path(monkeypatch):
    monkeypatch.setattr(render_gate.shutil, "which", lambda _name: None)
    reason = render_gate._soffice_unavailable_reason()
    assert reason is not None
    assert "soffice" in reason.lower() or "libreoffice" in reason.lower()


def test_soffice_backend_available_when_on_path(monkeypatch):
    monkeypatch.setattr(
        render_gate.shutil, "which", lambda name: "/usr/bin/soffice" if name == "soffice" else None
    )
    assert render_gate._soffice_unavailable_reason() is None


def test_word_com_unavailable_reason_off_windows(monkeypatch):
    monkeypatch.setattr(render_gate.sys, "platform", "linux")
    reason = render_gate._word_com_unavailable_reason()
    assert reason is not None
    assert "win32" in reason.lower()


# ---------------------------------------------------------------------------
# Wiring: the MCP tool in server.py must delegate to render_gate, unmodified,
# so the same three-state contract survives the tool boundary.
# ---------------------------------------------------------------------------

def test_server_tool_delegates_to_render_gate(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    sentinel = {"status": render_gate.FAILED, "reason": "sentinel from monkeypatch"}
    seen_paths: list[str] = []

    def _fake_check(path: str, **kwargs: Any) -> dict[str, Any]:
        seen_paths.append(path)
        return sentinel

    monkeypatch.setattr(render_gate, "check_render_capability", _fake_check)

    result = server.check_render_capability(docx_path)

    assert result is sentinel
    assert seen_paths == [docx_path]


def test_server_tool_is_registered_as_an_mcp_tool():
    import inspect

    assert callable(server.check_render_capability)
    sig = inspect.signature(server.check_render_capability)
    assert list(sig.parameters) == ["docx_path"]


# ---------------------------------------------------------------------------
# verify_promotion_readiness (c7cc9da4) -- fingerprint equality + structural/
# render verification gate for a review session's draft/overlay promotion.
# ---------------------------------------------------------------------------

def test_verify_promotion_readiness_ready_when_fingerprint_and_structure_hold(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx", paragraph_count=2)
    draft_path = _write_real_docx(tmp_path, "draft.docx", paragraph_count=3)
    expected = _sha256_of(canonical_path)

    result = render_gate.verify_promotion_readiness(
        canonical_path, draft_path, expected, backends=[]
    )

    assert result["ready"] is True
    assert result["reason"] is None
    assert result["fingerprint_check"]["match"] is True
    assert result["fingerprint_check"]["expected_source_sha256"] == expected
    assert result["structural_check"]["dropped_families"] == []
    # backends=[] -> render backend genuinely unavailable in this test env;
    # that must NOT block readiness when require_render defaults to False.
    assert result["render_check"]["status"] == render_gate.UNAVAILABLE_WITH_REASON


def test_verify_promotion_readiness_refuses_on_fingerprint_mismatch(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx")
    draft_path = _write_real_docx(tmp_path, "draft.docx")

    result = render_gate.verify_promotion_readiness(
        canonical_path, draft_path, "0" * 64, backends=[]
    )

    assert result["ready"] is False
    assert result["fingerprint_check"]["match"] is False
    assert "changed on disk" in result["reason"]


def test_verify_promotion_readiness_refuses_when_draft_drops_media(tmp_path):
    canonical_path = _write_real_docx(
        tmp_path, "canonical.docx", media_files=["image1.png"]
    )
    draft_path = _write_real_docx(tmp_path, "draft.docx", media_files=[])
    expected = _sha256_of(canonical_path)

    result = render_gate.verify_promotion_readiness(
        canonical_path, draft_path, expected, backends=[]
    )

    assert result["ready"] is False
    assert "media_count" in result["structural_check"]["dropped_families"]
    assert "media_count" in result["reason"]


def test_verify_promotion_readiness_refuses_when_draft_drops_a_table(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx", table_count=1)
    draft_path = _write_real_docx(tmp_path, "draft.docx", table_count=0)
    expected = _sha256_of(canonical_path)

    result = render_gate.verify_promotion_readiness(
        canonical_path, draft_path, expected, backends=[]
    )

    assert result["ready"] is False
    assert "table_count" in result["structural_check"]["dropped_families"]


def test_verify_promotion_readiness_adding_structure_is_fine(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx", media_files=[])
    draft_path = _write_real_docx(tmp_path, "draft.docx", media_files=["new.png"])
    expected = _sha256_of(canonical_path)

    result = render_gate.verify_promotion_readiness(
        canonical_path, draft_path, expected, backends=[]
    )

    assert result["ready"] is True
    assert result["structural_check"]["dropped_families"] == []


def test_verify_promotion_readiness_require_render_blocks_when_unavailable(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx")
    draft_path = _write_real_docx(tmp_path, "draft.docx")
    expected = _sha256_of(canonical_path)

    result = render_gate.verify_promotion_readiness(
        canonical_path, draft_path, expected, require_render=True, backends=[]
    )

    assert result["ready"] is False
    assert "require_render" in result["reason"]
    assert result["render_check"]["status"] == render_gate.UNAVAILABLE_WITH_REASON


def test_verify_promotion_readiness_require_render_passes_when_rendered(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx")
    draft_path = _write_real_docx(tmp_path, "draft.docx")
    expected = _sha256_of(canonical_path)
    fake_backend = _fake_backend("fake-ok", available=True, render=lambda path: {"ok": True})

    result = render_gate.verify_promotion_readiness(
        canonical_path,
        draft_path,
        expected,
        require_render=True,
        backends=[fake_backend],
    )

    assert result["ready"] is True
    assert result["render_check"]["status"] == render_gate.RENDERED


def test_verify_promotion_readiness_never_mutates_either_file(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx")
    draft_path = _write_real_docx(tmp_path, "draft.docx")
    expected = _sha256_of(canonical_path)
    canonical_before = _sha256_of(canonical_path)
    draft_before = _sha256_of(draft_path)

    render_gate.verify_promotion_readiness(
        canonical_path, draft_path, "0" * 64, backends=[]
    )
    render_gate.verify_promotion_readiness(
        canonical_path, draft_path, expected, require_render=True, backends=[]
    )

    assert _sha256_of(canonical_path) == canonical_before
    assert _sha256_of(draft_path) == draft_before


def test_verify_promotion_readiness_fails_closed_on_missing_arguments(tmp_path):
    canonical_path = _write_real_docx(tmp_path, "canonical.docx")

    assert render_gate.verify_promotion_readiness("", "draft.docx", "abc")["ready"] is False
    assert render_gate.verify_promotion_readiness(canonical_path, "", "abc")["ready"] is False
    assert render_gate.verify_promotion_readiness(canonical_path, "draft.docx", "")["ready"] is False


def test_verify_promotion_readiness_fails_closed_on_missing_files(tmp_path):
    missing = str(tmp_path / "does_not_exist.docx")
    draft_path = _write_real_docx(tmp_path, "draft.docx")

    canonical_missing = render_gate.verify_promotion_readiness(
        missing, draft_path, "a" * 64, backends=[]
    )
    assert canonical_missing["ready"] is False
    assert "canonical_path" in canonical_missing["reason"]

    canonical_path = _write_real_docx(tmp_path, "canonical.docx")
    expected = _sha256_of(canonical_path)
    draft_missing = render_gate.verify_promotion_readiness(
        canonical_path, missing, expected, backends=[]
    )
    assert draft_missing["ready"] is False
    assert draft_missing["structural_check"]["error"]
