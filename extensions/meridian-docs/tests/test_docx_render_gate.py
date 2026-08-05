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
import os
import subprocess
import sys
import time
import zipfile
from typing import Any, Callable

import pytest

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


# ---------------------------------------------------------------------------
# c44d245d -- bounded, diagnostic failure classification (timeout / transport
# / corruption), stderr/exit-code capture, and bounded retry of retryable-only
# failures on top of the three-state contract above (unchanged).
# ---------------------------------------------------------------------------


def test_render_capability_error_defaults_are_backward_compatible():
    """Existing ``raise RenderCapabilityError("message")`` call sites (no
    classification kwargs) must keep working -- default to UNKNOWN_ERROR,
    non-retryable, no exit_code/stderr/timed_out evidence."""
    exc = render_gate.RenderCapabilityError("plain message")
    assert str(exc) == "plain message"
    assert exc.error_class == render_gate.UNKNOWN_ERROR
    assert exc.exit_code is None
    assert exc.stderr is None
    assert exc.timed_out is False
    assert exc.retryable is False


def test_render_capability_error_rejects_unknown_error_class():
    exc = render_gate.RenderCapabilityError("msg", error_class="not-a-real-class")
    assert exc.error_class == render_gate.UNKNOWN_ERROR


def test_failure_classes_constant_has_the_four_expected_members():
    assert render_gate.FAILURE_CLASSES == (
        render_gate.TIMEOUT_ERROR,
        render_gate.TRANSPORT_ERROR,
        render_gate.CORRUPTION_ERROR,
        render_gate.UNKNOWN_ERROR,
    )
    assert render_gate.TIMEOUT_ERROR == "timeout"
    assert render_gate.TRANSPORT_ERROR == "transport"
    assert render_gate.CORRUPTION_ERROR == "corruption"
    assert render_gate.UNKNOWN_ERROR == "unknown"


# --- check_render_capability: generic (backend-agnostic) retry contract ----


def test_check_render_capability_retries_a_retryable_failure_and_recovers(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise render_gate.RenderCapabilityError(
                "transient hiccup", error_class=render_gate.TRANSPORT_ERROR, retryable=True
            )
        return {"converted_via": "fake", "attempt": len(calls)}

    backend = _fake_backend("flaky", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == render_gate.RENDERED
    assert result["detail"]["attempt"] == 2
    assert len(calls) == 2


def test_check_render_capability_never_retries_a_non_retryable_failure(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise render_gate.RenderCapabilityError(
            "document is corrupt", error_class=render_gate.CORRUPTION_ERROR, retryable=False
        )

    backend = _fake_backend("broken-doc", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend], max_retries=5)

    assert result["status"] == render_gate.FAILED
    assert len(calls) == 1, "a non-retryable failure must never be retried, regardless of max_retries"
    assert result["detail"]["error_class"] == render_gate.CORRUPTION_ERROR
    assert result["detail"]["attempts"] == 1


def test_check_render_capability_bounds_retries_at_max_retries(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise render_gate.RenderCapabilityError(
            "always transient", error_class=render_gate.TRANSPORT_ERROR, retryable=True
        )

    backend = _fake_backend("always-flaky", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend], max_retries=2)

    assert result["status"] == render_gate.FAILED
    assert len(calls) == 3, "max_retries=2 means 1 initial attempt + 2 retries = 3 total calls"
    assert result["detail"]["attempts"] == 3
    assert result["detail"]["error_class"] == render_gate.TRANSPORT_ERROR


def test_check_render_capability_default_max_retries_is_one(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise render_gate.RenderCapabilityError(
            "always transient", error_class=render_gate.TRANSPORT_ERROR, retryable=True
        )

    backend = _fake_backend("always-flaky", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == render_gate.FAILED
    assert len(calls) == 2, "default max_retries=1 means 1 initial attempt + 1 retry = 2 total calls"


def test_check_render_capability_timeout_failure_detail_is_never_retried(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise render_gate.RenderCapabilityError(
            "hung", error_class=render_gate.TIMEOUT_ERROR, timed_out=True, retryable=False
        )

    backend = _fake_backend("hangs", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend], max_retries=5)

    assert result["status"] == render_gate.FAILED
    assert len(calls) == 1
    assert result["detail"]["error_class"] == render_gate.TIMEOUT_ERROR
    assert result["detail"]["timed_out"] is True


def test_check_render_capability_failed_detail_carries_exit_code_and_stderr(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)

    def _render(path: str) -> dict[str, Any]:
        raise render_gate.RenderCapabilityError(
            "exit 1", error_class=render_gate.CORRUPTION_ERROR, exit_code=1, stderr="bad zip"
        )

    backend = _fake_backend("bad-exit", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend])

    assert result["detail"]["exit_code"] == 1
    assert result["detail"]["stderr"] == "bad zip"


def test_check_render_capability_unclassified_exception_is_never_retried(tmp_path):
    """An exception that ISN'T a RenderCapabilityError (a genuine backend
    bug) has no classification/retryable signal at all -- never retried,
    reported as UNKNOWN_ERROR with the raw exception type recorded."""
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise ValueError("totally unexpected bug")

    backend = _fake_backend("buggy", available=True, render=_render)

    result = render_gate.check_render_capability(docx_path, backends=[backend], max_retries=5)

    assert result["status"] == render_gate.FAILED
    assert len(calls) == 1
    assert result["detail"]["error_class"] == render_gate.UNKNOWN_ERROR
    assert result["detail"]["exception_type"] == "ValueError"


# --- _soffice_render: real classification behavior -------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""


def test_classify_soffice_failure_corruption_marker():
    error_class, retryable = render_gate._classify_soffice_failure(
        "Error: source file could not be loaded!"
    )
    assert error_class == render_gate.CORRUPTION_ERROR
    assert retryable is False


def test_classify_soffice_failure_default_is_transport_and_retryable():
    error_class, retryable = render_gate._classify_soffice_failure(
        "convert /tmp/profile: lock held by another instance"
    )
    assert error_class == render_gate.TRANSPORT_ERROR
    assert retryable is True


def test_classify_soffice_failure_empty_stderr_is_transport():
    error_class, retryable = render_gate._classify_soffice_failure("")
    assert error_class == render_gate.TRANSPORT_ERROR
    assert retryable is True


def test_soffice_render_timeout_is_classified_and_carries_stderr(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(render_gate, "_soffice_executable", lambda: "/usr/bin/soffice")

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"), output=b"", stderr=b"stuck")

    monkeypatch.setattr(render_gate.subprocess, "run", _fake_run)

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._soffice_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == render_gate.TIMEOUT_ERROR
    assert exc.timed_out is True
    assert exc.retryable is False
    assert exc.stderr == "stuck"


def test_soffice_render_spawn_failure_is_transport_and_retryable(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(render_gate, "_soffice_executable", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        render_gate.subprocess, "run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(OSError("could not spawn")),
    )

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._soffice_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == render_gate.TRANSPORT_ERROR
    assert exc.retryable is True


def test_soffice_render_nonzero_exit_with_corruption_marker(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(render_gate, "_soffice_executable", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        render_gate.subprocess, "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(1, stderr=b"source file could not be loaded"),
    )

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._soffice_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == render_gate.CORRUPTION_ERROR
    assert exc.retryable is False
    assert exc.exit_code == 1
    assert exc.stderr == "source file could not be loaded"


def test_soffice_render_retries_through_check_render_capability_and_recovers(tmp_path, monkeypatch):
    """End-to-end: a transient soffice spawn failure followed by a successful
    conversion recovers via check_render_capability's retry, exercising the
    REAL _soffice_render backend (not a fake stand-in)."""
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(render_gate, "_soffice_executable", lambda: "/usr/bin/soffice")

    calls: list[int] = []

    def _fake_run(cmd, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient spawn failure")
        out_dir = cmd[5]
        with open(os.path.join(out_dir, "doc.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(render_gate.subprocess, "run", _fake_run)

    result = render_gate.check_render_capability(docx_path, backends=[render_gate._SOFFICE_BACKEND])

    assert result["status"] == render_gate.RENDERED
    assert result["detail"]["converted_via"] == "soffice"
    assert len(calls) == 2


# --- _word_com_render: bounded timeout, owned-process cleanup, COM error ---
# classification. No real pywin32/Word dependency needed -- fake win32com /
# win32process modules are injected directly into sys.modules so this is
# fully deterministic on any platform/machine.


class _FakeWordDocument:
    def __init__(self, on_save):
        self._on_save = on_save
        self.closed_with = None

    def SaveAs(self, path, FileFormat):
        self._on_save(path)

    def Close(self, save_changes):
        self.closed_with = save_changes


class _FakeWordApplication:
    def __init__(self, hwnd, open_document):
        self.Hwnd = hwnd
        self.Visible = None
        self._open_document = open_document
        self.quit_called = False

    class _Documents:
        def __init__(self, outer):
            self._outer = outer

        def Open(self, path, ReadOnly=True):
            return self._outer._open_document(path)

    @property
    def Documents(self):
        return self._Documents(self)

    def Quit(self):
        self.quit_called = True


def _install_fake_win32com(monkeypatch, open_document, hwnd=4242):
    """Inject fake ``win32com.client`` / ``win32process`` modules into
    sys.modules so ``_word_com_render``'s local ``import win32com.client``
    (and ``_word_application_pid``'s local ``import win32process``) bind to
    these fakes instead of requiring real pywin32 -- works on any platform.
    """
    import types

    fake_client = types.ModuleType("win32com.client")
    app_holder: dict[str, Any] = {}

    def _dispatch_ex(prog_id):
        app = _FakeWordApplication(hwnd, open_document)
        app_holder["app"] = app
        return app

    fake_client.DispatchEx = _dispatch_ex

    fake_win32com = types.ModuleType("win32com")
    fake_win32com.client = fake_client

    fake_win32process = types.ModuleType("win32process")
    fake_win32process.GetWindowThreadProcessId = lambda hwnd_arg: (0, hwnd_arg)

    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
    return app_holder


def test_word_com_render_happy_path_produces_pdf(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)

    def _open_document(path):
        def _on_save(pdf_path):
            with open(pdf_path, "wb") as fh:
                fh.write(b"%PDF-1.4 fake")
        return _FakeWordDocument(_on_save)

    app_holder = _install_fake_win32com(monkeypatch, _open_document)
    terminate_calls: list[int] = []
    monkeypatch.setattr(render_gate, "_terminate_owned_process", lambda pid: terminate_calls.append(pid) or True)

    result = render_gate._word_com_render(docx_path)

    assert result["converted_via"] == "word-com"
    assert app_holder["app"].quit_called is True
    assert terminate_calls == [], "a successful render must never trigger owned-process cleanup"


def test_word_com_render_bounded_timeout_terminates_owned_process(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(render_gate, "_WORD_COM_TIMEOUT_SECONDS", 0.05)

    def _open_document(path):
        time.sleep(2.0)  # exceeds the shrunk bound -- watchdog must fire first
        return _FakeWordDocument(lambda pdf_path: None)

    _install_fake_win32com(monkeypatch, _open_document)
    terminate_calls: list[int] = []
    monkeypatch.setattr(
        render_gate, "_terminate_owned_process",
        lambda pid: terminate_calls.append(pid) or True,
    )

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._word_com_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == render_gate.TIMEOUT_ERROR
    assert exc.timed_out is True
    assert exc.retryable is False
    assert terminate_calls == [4242], (
        "timeout must terminate ONLY the exact owned pid this call resolved, "
        "never a process sweep"
    )


def test_word_com_render_no_owned_pid_skips_cleanup_but_still_times_out(tmp_path, monkeypatch):
    """When the owned pid can't be resolved (e.g. win32process unavailable),
    cleanup must be skipped gracefully -- never raise -- while the timeout
    itself is still reported."""
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(render_gate, "_WORD_COM_TIMEOUT_SECONDS", 0.05)

    def _open_document(path):
        time.sleep(2.0)
        return _FakeWordDocument(lambda pdf_path: None)

    _install_fake_win32com(monkeypatch, _open_document)
    monkeypatch.setattr(render_gate, "_word_application_pid", lambda word: None)
    terminate_calls: list[int] = []
    monkeypatch.setattr(
        render_gate, "_terminate_owned_process",
        lambda pid: terminate_calls.append(pid) or True,
    )

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._word_com_render(docx_path)

    assert excinfo.value.error_class == render_gate.TIMEOUT_ERROR
    assert terminate_calls == []


def test_word_com_render_document_open_raises_is_classified(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)

    def _open_document(path):
        raise Exception("RPC server is unavailable.")

    _install_fake_win32com(monkeypatch, _open_document)

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._word_com_render(docx_path)

    assert excinfo.value.error_class == render_gate.TRANSPORT_ERROR
    assert excinfo.value.retryable is True


def test_word_com_render_no_pdf_written_is_unknown_not_rendered(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)

    def _open_document(path):
        return _FakeWordDocument(lambda pdf_path: None)  # never writes the PDF

    _install_fake_win32com(monkeypatch, _open_document)

    with pytest.raises(render_gate.RenderCapabilityError) as excinfo:
        render_gate._word_com_render(docx_path)

    assert excinfo.value.error_class == render_gate.UNKNOWN_ERROR
    assert excinfo.value.retryable is False


def test_classify_word_com_exception_transient():
    error_class, retryable = render_gate._classify_word_com_exception(
        Exception("The RPC server is unavailable.")
    )
    assert error_class == render_gate.TRANSPORT_ERROR
    assert retryable is True


def test_classify_word_com_exception_corruption():
    error_class, retryable = render_gate._classify_word_com_exception(
        Exception("Word cannot open the document: converter not found")
    )
    assert error_class == render_gate.CORRUPTION_ERROR
    assert retryable is False


def test_classify_word_com_exception_unknown_default():
    error_class, retryable = render_gate._classify_word_com_exception(
        Exception("something completely unrelated")
    )
    assert error_class == render_gate.UNKNOWN_ERROR
    assert retryable is False


def test_word_application_pid_returns_none_when_win32process_missing(monkeypatch):
    monkeypatch.delitem(sys.modules, "win32process", raising=False)

    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "win32process":
            raise ImportError("no pywin32 in this test environment")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    class _FakeApp:
        Hwnd = 1

    assert render_gate._word_application_pid(_FakeApp()) is None


def test_word_application_pid_resolves_via_win32process(monkeypatch):
    import types

    fake_win32process = types.ModuleType("win32process")
    fake_win32process.GetWindowThreadProcessId = lambda hwnd: (11, 9988)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)

    class _FakeApp:
        Hwnd = 1

    assert render_gate._word_application_pid(_FakeApp()) == 9988


def test_terminate_owned_process_success(monkeypatch):
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(render_gate.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert render_gate._terminate_owned_process(555) is True
    assert killed == [(555, render_gate.signal.SIGTERM)]


def test_terminate_owned_process_failure_returns_false_never_raises(monkeypatch):
    def _boom(pid, sig):
        raise OSError("no such process")

    monkeypatch.setattr(render_gate.os, "kill", _boom)

    assert render_gate._terminate_owned_process(555) is False


# --- server.py wiring: max_retries stays optional / defaulted through -----


def test_server_check_render_capability_still_works_with_new_retry_param(tmp_path, monkeypatch):
    """server.check_render_capability's signature is unchanged (docx_path
    only) -- max_retries is an internal render_gate default, not something
    every MCP caller needs to know about."""
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(
        render_gate, "check_render_capability",
        lambda path, **kwargs: {"status": render_gate.FAILED, "reason": "sentinel"},
    )

    result = server.check_render_capability(docx_path)

    assert result["status"] == render_gate.FAILED
