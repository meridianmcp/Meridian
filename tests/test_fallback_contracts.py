"""Tests for meridian/fallbacks -- render-capability detection (8d2ef784, DOCS-R2-A).

Provenance / scope note: an earlier, never-merged attempt at this port
(orphaned commit ``e5045b3f``, Aug 9 2026) fully duplicated BOTH backends,
including a second independent Word-COM ``pywin32`` automation
implementation. This suite instead matches ``meridian/fallbacks/__init__.py``
as actually shipped by 8d2ef784 (see that module's docstring for the full
rationale): the LibreOffice/soffice backend IS a genuine, independent
re-implementation (tested against real classification/retry behavior below,
same as the original port), but the Word-COM backend DELEGATES (guarded,
dotted ``importlib``) to
``meridian_docs.render_gate.check_word_com_render_receipt`` rather than
re-implementing a third copy of isolated-process Word/COM automation --
mirroring ``tools/meridian_fallbacks/docx_completion_gate.py``'s own
``_default_render_checker`` pattern. The delegation tests below are adapted
from that sibling module's test suite
(``tools/meridian_fallbacks/tests/test_docx_completion_gate.py``), not from
``e5045b3f``'s (now-obsolete) direct pywin32 fakery.

Everything else -- the three-state contract, bounded retry/timeout/
transport/corruption classification, and detect_backend/check_render_
capability's generic backend-agnostic behavior -- is unchanged from the
extension's own ``render_gate.py`` contract (sprint item 93cd9798 there) and
is exercised here without depending on LibreOffice or Word actually being
installed on whatever machine runs this suite (backends are injected via
``check_render_capability``'s ``backends=`` parameter, or the real soffice/
word-com backends are exercised with ``subprocess.run``/``importlib`` mocked
out).
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any, Callable

import pytest

from meridian import fallbacks


def _write_dummy_docx(tmp_path, name: str = "doc.docx") -> str:
    # fallbacks does not parse the file itself (that's the backend's job, and
    # our fake backends below don't touch file content) -- only its
    # existence/type is checked before a backend is even consulted.
    path = tmp_path / name
    path.write_bytes(b"not a real docx -- fallbacks never opens this itself")
    return str(path)


def _fake_backend(
    name: str,
    *,
    available: bool,
    reason: str | None = None,
    render: Callable[[str], dict[str, Any]] | None = None,
) -> fallbacks.RenderBackend:
    def _unavailable_reason() -> str | None:
        return None if available else reason

    return fallbacks.RenderBackend(
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

    result = fallbacks.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == fallbacks.RENDERED
    assert result["status"] == "rendered"
    assert result["backend"] == "fake-ok"
    assert result["detail"]["path_seen"] == docx_path
    # "rendered" is the ONLY status meaning "verified" -- guard against it
    # ever doubling as the other two.
    assert result["status"] != fallbacks.UNAVAILABLE_WITH_REASON
    assert result["status"] != fallbacks.FAILED


def test_first_available_backend_in_the_chain_wins(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    unavailable = _fake_backend("skipped", available=False, reason="not installed")
    winner = _fake_backend("winner", available=True, render=lambda path: {"which": "winner"})
    never_reached = _fake_backend(
        "never-reached",
        available=True,
        render=lambda path: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = fallbacks.check_render_capability(
        docx_path, backends=[unavailable, winner, never_reached]
    )

    assert result["status"] == fallbacks.RENDERED
    assert result["backend"] == "winner"
    assert result["detail"]["which"] == "winner"


# ---------------------------------------------------------------------------
# 2. capability missing -> "unavailable-with-reason" (real, itemized reason)
# ---------------------------------------------------------------------------


def test_no_backend_available_reports_itemized_real_reason(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    backend_a = _fake_backend("backend-a", available=False, reason="binary-a not on PATH")
    backend_b = _fake_backend("backend-b", available=False, reason="binary-b not installed")

    result = fallbacks.check_render_capability(docx_path, backends=[backend_a, backend_b])

    assert result["status"] == fallbacks.UNAVAILABLE_WITH_REASON
    assert result["status"] == "unavailable-with-reason"
    assert "reason" in result
    reason = result["reason"]
    assert reason.strip().lower() != "unavailable"
    assert reason.strip().lower() != "unavailable-with-reason"
    assert "backend-a" in reason and "binary-a not on PATH" in reason
    assert "backend-b" in reason and "binary-b not installed" in reason
    assert "backend" not in result  # no backend was actually selected
    assert result["status"] != fallbacks.RENDERED
    assert result["status"] != fallbacks.FAILED


def test_no_backends_registered_at_all_is_still_a_real_reason(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)

    result = fallbacks.check_render_capability(docx_path, backends=[])

    assert result["status"] == fallbacks.UNAVAILABLE_WITH_REASON
    assert "no render backend" in result["reason"].lower()


# ---------------------------------------------------------------------------
# 3. render attempt errors -> "failed" (never silently rendered/available)
# ---------------------------------------------------------------------------


def test_render_attempt_raises_capability_error_is_failed_not_rendered(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)

    def _boom(path: str) -> dict[str, Any]:
        raise fallbacks.RenderCapabilityError("soffice exited with code 1: corrupt zip")

    backend = _fake_backend("fake-broken", available=True, render=_boom)

    result = fallbacks.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == fallbacks.FAILED
    assert "corrupt zip" in result["reason"]
    assert result["backend"] == "fake-broken"
    # The critical invariant this whole module exists for: a real error must
    # NEVER be reported as success, and must NEVER be folded into "we simply
    # couldn't check" -- the two mean different things to a caller.
    assert result["status"] != fallbacks.RENDERED
    assert result["status"] != fallbacks.UNAVAILABLE_WITH_REASON


def test_unexpected_exception_from_backend_is_also_failed(tmp_path):
    """A backend can raise something other than RenderCapabilityError (e.g. a
    raw pywintypes.com_error out of Word COM, or an OSError from subprocess) --
    check_render_capability must still classify it as 'failed', never let it
    propagate uncaught and never count it as success."""
    docx_path = _write_dummy_docx(tmp_path)

    def _explode(path: str) -> dict[str, Any]:
        raise ValueError("totally unexpected backend bug")

    backend = _fake_backend("fake-buggy", available=True, render=_explode)

    result = fallbacks.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == fallbacks.FAILED
    assert "totally unexpected backend bug" in result["reason"]
    assert result["backend"] == "fake-buggy"


def test_empty_docx_path_is_failed(tmp_path):
    backend = _fake_backend("fake-ok", available=True, render=lambda path: {})
    result = fallbacks.check_render_capability("   ", backends=[backend])
    assert result["status"] == fallbacks.FAILED
    assert "non-empty string" in result["reason"]


def test_directory_path_is_failed_not_a_file(tmp_path):
    backend = _fake_backend("fake-ok", available=True, render=lambda path: {})
    result = fallbacks.check_render_capability(str(tmp_path), backends=[backend])
    assert result["status"] == fallbacks.FAILED
    assert "not a file" in result["reason"]


def test_render_backend_is_available_convenience_method():
    available = _fake_backend("avail", available=True)
    unavailable = _fake_backend("unavail", available=False, reason="nope")
    assert available.is_available() is True
    assert unavailable.is_available() is False


def test_missing_file_is_failed_not_unavailable(tmp_path):
    """A bad docx_path is a concrete, checkable error about THIS call --
    distinct from 'unavailable-with-reason', which is reserved for
    environment-level capability gaps true regardless of which document was
    passed in."""
    missing_path = str(tmp_path / "does_not_exist.docx")
    backend = _fake_backend("fake-ok", available=True, render=lambda path: {})

    result = fallbacks.check_render_capability(missing_path, backends=[backend])

    assert result["status"] == fallbacks.FAILED
    assert "no such file" in result["reason"]


# ---------------------------------------------------------------------------
# detect_backend -- the itemization helper the "unavailable" path relies on.
# ---------------------------------------------------------------------------


def test_detect_backend_returns_first_available_and_collected_reasons():
    unavailable = _fake_backend("unavailable-one", available=False, reason="missing binary")
    available = _fake_backend("available-one", available=True)

    backend, reasons = fallbacks.detect_backend([unavailable, available])

    assert backend is not None
    assert backend.name == "available-one"
    assert any("unavailable-one" in r and "missing binary" in r for r in reasons)


def test_detect_backend_none_available():
    unavailable = _fake_backend("only-one", available=False, reason="not installed")

    backend, reasons = fallbacks.detect_backend([unavailable])

    assert backend is None
    assert reasons == ["only-one: not installed"]


def test_render_statuses_constant_matches_the_three_state_contract():
    assert fallbacks.RENDER_STATUSES == (
        fallbacks.RENDERED,
        fallbacks.UNAVAILABLE_WITH_REASON,
        fallbacks.FAILED,
    )
    assert fallbacks.RENDERED == "rendered"
    assert fallbacks.UNAVAILABLE_WITH_REASON == "unavailable-with-reason"
    assert fallbacks.FAILED == "failed"


# ---------------------------------------------------------------------------
# Default (real) backend registration.
# ---------------------------------------------------------------------------


def test_default_backends_are_registered_and_named():
    names = [backend.name for backend in fallbacks.KNOWN_BACKENDS]
    assert names == ["libreoffice-soffice", "word-com"], (
        "soffice must be tried before word-com, matching "
        "extensions/meridian-docs/meridian_docs/render_gate.py's own order"
    )


def test_soffice_backend_unavailable_reason_when_not_on_path(monkeypatch):
    monkeypatch.setattr(fallbacks.shutil, "which", lambda _name: None)
    reason = fallbacks._soffice_unavailable_reason()
    assert reason is not None
    assert "soffice" in reason.lower() or "libreoffice" in reason.lower()


def test_soffice_backend_available_when_on_path(monkeypatch):
    monkeypatch.setattr(
        fallbacks.shutil, "which", lambda name: "/usr/bin/soffice" if name == "soffice" else None
    )
    assert fallbacks._soffice_unavailable_reason() is None


# ---------------------------------------------------------------------------
# RenderCapabilityError / FAILURE_CLASSES -- backward-compatible defaults.
# ---------------------------------------------------------------------------


def test_render_capability_error_defaults_are_backward_compatible():
    """A plain ``raise RenderCapabilityError("message")`` call site (no
    classification kwargs) must keep working -- default to UNKNOWN_ERROR,
    non-retryable, no exit_code/stderr/timed_out evidence."""
    exc = fallbacks.RenderCapabilityError("plain message")
    assert str(exc) == "plain message"
    assert exc.error_class == fallbacks.UNKNOWN_ERROR
    assert exc.exit_code is None
    assert exc.stderr is None
    assert exc.timed_out is False
    assert exc.retryable is False


def test_render_capability_error_rejects_unknown_error_class():
    exc = fallbacks.RenderCapabilityError("msg", error_class="not-a-real-class")
    assert exc.error_class == fallbacks.UNKNOWN_ERROR


def test_failure_classes_constant_has_the_four_expected_members():
    assert fallbacks.FAILURE_CLASSES == (
        fallbacks.TIMEOUT_ERROR,
        fallbacks.TRANSPORT_ERROR,
        fallbacks.CORRUPTION_ERROR,
        fallbacks.UNKNOWN_ERROR,
    )
    assert fallbacks.TIMEOUT_ERROR == "timeout"
    assert fallbacks.TRANSPORT_ERROR == "transport"
    assert fallbacks.CORRUPTION_ERROR == "corruption"
    assert fallbacks.UNKNOWN_ERROR == "unknown"


# --- check_render_capability: generic (backend-agnostic) retry contract ----


def test_check_render_capability_retries_a_retryable_failure_and_recovers(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise fallbacks.RenderCapabilityError(
                "transient hiccup", error_class=fallbacks.TRANSPORT_ERROR, retryable=True
            )
        return {"converted_via": "fake", "attempt": len(calls)}

    backend = _fake_backend("flaky", available=True, render=_render)

    result = fallbacks.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == fallbacks.RENDERED
    assert result["detail"]["attempt"] == 2
    assert len(calls) == 2


def test_check_render_capability_never_retries_a_non_retryable_failure(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise fallbacks.RenderCapabilityError(
            "document is corrupt", error_class=fallbacks.CORRUPTION_ERROR, retryable=False
        )

    backend = _fake_backend("broken-doc", available=True, render=_render)

    result = fallbacks.check_render_capability(docx_path, backends=[backend], max_retries=5)

    assert result["status"] == fallbacks.FAILED
    assert len(calls) == 1, "a non-retryable failure must never be retried, regardless of max_retries"
    assert result["detail"]["error_class"] == fallbacks.CORRUPTION_ERROR
    assert result["detail"]["attempts"] == 1


def test_check_render_capability_bounds_retries_at_max_retries(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise fallbacks.RenderCapabilityError(
            "always transient", error_class=fallbacks.TRANSPORT_ERROR, retryable=True
        )

    backend = _fake_backend("always-flaky", available=True, render=_render)

    result = fallbacks.check_render_capability(docx_path, backends=[backend], max_retries=2)

    assert result["status"] == fallbacks.FAILED
    assert len(calls) == 3, "max_retries=2 means 1 initial attempt + 2 retries = 3 total calls"
    assert result["detail"]["attempts"] == 3
    assert result["detail"]["error_class"] == fallbacks.TRANSPORT_ERROR


def test_check_render_capability_default_max_retries_is_one(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise fallbacks.RenderCapabilityError(
            "always transient", error_class=fallbacks.TRANSPORT_ERROR, retryable=True
        )

    backend = _fake_backend("always-flaky", available=True, render=_render)

    result = fallbacks.check_render_capability(docx_path, backends=[backend])

    assert result["status"] == fallbacks.FAILED
    assert len(calls) == 2, "default max_retries=1 means 1 initial attempt + 1 retry = 2 total calls"


def test_check_render_capability_timeout_failure_detail_is_never_retried(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    calls: list[int] = []

    def _render(path: str) -> dict[str, Any]:
        calls.append(1)
        raise fallbacks.RenderCapabilityError(
            "hung", error_class=fallbacks.TIMEOUT_ERROR, timed_out=True, retryable=False
        )

    backend = _fake_backend("hangs", available=True, render=_render)

    result = fallbacks.check_render_capability(docx_path, backends=[backend], max_retries=5)

    assert result["status"] == fallbacks.FAILED
    assert len(calls) == 1
    assert result["detail"]["error_class"] == fallbacks.TIMEOUT_ERROR
    assert result["detail"]["timed_out"] is True


def test_check_render_capability_failed_detail_carries_exit_code_and_stderr(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)

    def _render(path: str) -> dict[str, Any]:
        raise fallbacks.RenderCapabilityError(
            "exit 1", error_class=fallbacks.CORRUPTION_ERROR, exit_code=1, stderr="bad zip"
        )

    backend = _fake_backend("bad-exit", available=True, render=_render)

    result = fallbacks.check_render_capability(docx_path, backends=[backend])

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

    result = fallbacks.check_render_capability(docx_path, backends=[backend], max_retries=5)

    assert result["status"] == fallbacks.FAILED
    assert len(calls) == 1
    assert result["detail"]["error_class"] == fallbacks.UNKNOWN_ERROR
    assert result["detail"]["exception_type"] == "ValueError"


def test_check_render_capability_tags_backend_order_on_every_status(tmp_path):
    docx_path = _write_dummy_docx(tmp_path)
    unavailable = _fake_backend("first", available=False, reason="nope")
    winner = _fake_backend("second", available=True, render=lambda path: {})

    result = fallbacks.check_render_capability(docx_path, backends=[unavailable, winner])

    assert result["backend_order"] == ["first", "second"]


# --- _soffice_render: real classification behavior -------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""


def test_classify_soffice_failure_corruption_marker():
    error_class, retryable = fallbacks._classify_soffice_failure(
        "Error: source file could not be loaded!"
    )
    assert error_class == fallbacks.CORRUPTION_ERROR
    assert retryable is False


def test_classify_soffice_failure_default_is_transport_and_retryable():
    error_class, retryable = fallbacks._classify_soffice_failure(
        "convert /tmp/profile: lock held by another instance"
    )
    assert error_class == fallbacks.TRANSPORT_ERROR
    assert retryable is True


def test_classify_soffice_failure_empty_stderr_is_transport():
    error_class, retryable = fallbacks._classify_soffice_failure("")
    assert error_class == fallbacks.TRANSPORT_ERROR
    assert retryable is True


def test_soffice_render_timeout_is_classified_and_carries_stderr(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(fallbacks, "_soffice_executable", lambda: "/usr/bin/soffice")

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"), output=b"", stderr=b"stuck")

    monkeypatch.setattr(fallbacks.subprocess, "run", _fake_run)

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._soffice_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == fallbacks.TIMEOUT_ERROR
    assert exc.timed_out is True
    assert exc.retryable is False
    assert exc.stderr == "stuck"


def test_soffice_render_spawn_failure_is_transport_and_retryable(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(fallbacks, "_soffice_executable", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        fallbacks.subprocess, "run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(OSError("could not spawn")),
    )

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._soffice_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == fallbacks.TRANSPORT_ERROR
    assert exc.retryable is True


def test_soffice_render_nonzero_exit_with_corruption_marker(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(fallbacks, "_soffice_executable", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        fallbacks.subprocess, "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(1, stderr=b"source file could not be loaded"),
    )

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._soffice_render(docx_path)

    exc = excinfo.value
    assert exc.error_class == fallbacks.CORRUPTION_ERROR
    assert exc.retryable is False
    assert exc.exit_code == 1
    assert exc.stderr == "source file could not be loaded"


def test_soffice_render_no_pdf_produced_is_unknown_error(tmp_path, monkeypatch):
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(fallbacks, "_soffice_executable", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        fallbacks.subprocess, "run", lambda cmd, **kwargs: _FakeCompletedProcess(0),
    )

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._soffice_render(docx_path)

    assert excinfo.value.error_class == fallbacks.UNKNOWN_ERROR


def test_soffice_render_retries_through_check_render_capability_and_recovers(tmp_path, monkeypatch):
    """End-to-end: a transient soffice spawn failure followed by a successful
    conversion recovers via check_render_capability's retry, exercising the
    REAL _soffice_render backend (not a fake stand-in)."""
    import os

    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(fallbacks, "_soffice_executable", lambda: "/usr/bin/soffice")

    calls: list[int] = []

    def _fake_run(cmd, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient spawn failure")
        out_dir = cmd[5]
        with open(os.path.join(out_dir, "doc.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(fallbacks.subprocess, "run", _fake_run)

    result = fallbacks.check_render_capability(docx_path, backends=[fallbacks._SOFFICE_BACKEND])

    assert result["status"] == fallbacks.RENDERED
    assert result["detail"]["converted_via"] == "soffice"
    assert "pid" not in result["detail"], (
        "deliberately narrower than render_gate.py's own soffice backend -- "
        "no PDF hash/page-count receipt fields, this exists to gate a write, "
        "not to build a durable visual-QA receipt"
    )
    assert len(calls) == 2


def test_soffice_render_disappeared_executable_is_transport_and_retryable(tmp_path, monkeypatch):
    """unavailable_reason() should have prevented this call; guards anyway
    against a PATH-changed-mid-process race."""
    docx_path = _write_dummy_docx(tmp_path)
    monkeypatch.setattr(fallbacks, "_soffice_executable", lambda: None)

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._soffice_render(docx_path)

    assert excinfo.value.error_class == fallbacks.TRANSPORT_ERROR
    assert excinfo.value.retryable is True


# ---------------------------------------------------------------------------
# Word-COM backend -- DELEGATED (guarded importlib), not duplicated. See
# module docstring: adapted from
# tools/meridian_fallbacks/tests/test_docx_completion_gate.py's
# _default_render_checker tests, the sibling module this design matches.
# ---------------------------------------------------------------------------


def test_word_com_delegate_unavailable_reason_off_windows(monkeypatch):
    monkeypatch.setattr(fallbacks.sys, "platform", "linux")
    reason = fallbacks._word_com_delegate_unavailable_reason()
    assert reason is not None
    assert "win32" in reason.lower()


def _block_import(monkeypatch, blocked_name: str):
    """Force ``import <blocked_name>`` to raise ImportError, leaving every
    other import (including meridian_docs, when a caller separately
    monkeypatches importlib.import_module) unaffected. Mirrors the pattern
    tools/meridian_fallbacks' own test suite uses to fake a missing pywin32
    without needing it actually absent from the test environment."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == blocked_name or name.startswith(blocked_name + "."):
            raise ImportError(f"no {blocked_name} in this test environment")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def test_word_com_delegate_unavailable_reason_no_pywin32(monkeypatch):
    monkeypatch.setattr(fallbacks.sys, "platform", "win32")
    _block_import(monkeypatch, "win32com")

    reason = fallbacks._word_com_delegate_unavailable_reason()

    assert reason is not None
    assert "pywin32" in reason.lower()


def test_word_com_delegate_unavailable_reason_meridian_docs_not_importable(monkeypatch):
    monkeypatch.setattr(fallbacks.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "win32com.client", object())
    monkeypatch.setitem(sys.modules, "win32com", object())

    def _raise(name):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(fallbacks.importlib, "import_module", _raise)

    reason = fallbacks._word_com_delegate_unavailable_reason()

    assert reason is not None
    assert "not importable" in reason
    assert "meridian_docs" in reason


def test_word_com_delegate_unavailable_reason_missing_checker_attribute(monkeypatch):
    monkeypatch.setattr(fallbacks.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "win32com.client", object())
    monkeypatch.setitem(sys.modules, "win32com", object())

    class _FakeModule:
        pass

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    reason = fallbacks._word_com_delegate_unavailable_reason()

    assert reason is not None
    assert "check_word_com_render_receipt" in reason


def test_word_com_delegate_unavailable_reason_available(monkeypatch):
    monkeypatch.setattr(fallbacks.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "win32com.client", object())
    monkeypatch.setitem(sys.modules, "win32com", object())

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {"status": "rendered"}

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    assert fallbacks._word_com_delegate_unavailable_reason() is None


def test_word_com_delegate_render_maps_rendered(tmp_path, monkeypatch):
    path = _write_dummy_docx(tmp_path)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            assert docx_path == path
            return {"status": "rendered", "backend": "word-com", "detail": {"pages": 3}}

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    detail = fallbacks._word_com_delegate_render(path)

    assert detail["pages"] == 3
    assert detail["delegate_backend"] == "word-com"


def test_word_com_delegate_render_maps_failed_preserving_classification(tmp_path, monkeypatch):
    path = _write_dummy_docx(tmp_path)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {
                "status": "failed",
                "reason": "com error: RPC server is unavailable",
                "detail": {
                    "error_class": fallbacks.TRANSPORT_ERROR,
                    "exit_code": None,
                    "stderr": None,
                    "timed_out": False,
                },
            }

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._word_com_delegate_render(path)

    assert excinfo.value.error_class == fallbacks.TRANSPORT_ERROR
    assert "RPC server is unavailable" in str(excinfo.value)


def test_word_com_delegate_render_maps_unavailable_with_reason_to_failed(tmp_path, monkeypatch):
    """The cheap unavailable_reason() pre-check filters most cases before
    .render() is ever called; this covers the rare edge where the delegate
    STILL reports unavailable at call time (e.g. Word itself isn't
    installed even though pywin32 is) -- folded into 'failed' here rather
    than silently treated as available."""
    path = _write_dummy_docx(tmp_path)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {"status": "unavailable-with-reason", "reason": "Word is not installed"}

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._word_com_delegate_render(path)

    assert "Word is not installed" in str(excinfo.value)


def test_word_com_delegate_render_raising_checker_is_unknown_error(tmp_path, monkeypatch):
    path = _write_dummy_docx(tmp_path)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            raise RuntimeError("boom")

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._word_com_delegate_render(path)

    assert excinfo.value.error_class == fallbacks.UNKNOWN_ERROR
    assert "boom" in str(excinfo.value)


def test_word_com_delegate_render_unrecognized_error_class_falls_back_to_unknown(tmp_path, monkeypatch):
    path = _write_dummy_docx(tmp_path)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return {
                "status": "failed",
                "reason": "weird backend-specific code",
                "detail": {"error_class": "not-a-real-failure-class"},
            }

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._word_com_delegate_render(path)

    assert excinfo.value.error_class == fallbacks.UNKNOWN_ERROR


def test_word_com_delegate_render_non_dict_result_is_unknown_error(tmp_path, monkeypatch):
    path = _write_dummy_docx(tmp_path)

    class _FakeModule:
        @staticmethod
        def check_word_com_render_receipt(docx_path):
            return "not a dict"

    monkeypatch.setattr(fallbacks.importlib, "import_module", lambda name: _FakeModule())

    with pytest.raises(fallbacks.RenderCapabilityError) as excinfo:
        fallbacks._word_com_delegate_render(path)

    assert excinfo.value.error_class == fallbacks.UNKNOWN_ERROR


def test_check_word_com_render_receipt_scopes_to_word_com_backend_only(tmp_path, monkeypatch):
    """Mirrors render_gate.check_word_com_render_receipt's own narrowing
    helper -- must delegate to check_render_capability with ONLY the
    word-com backend in play, never the full KNOWN_BACKENDS chain (so
    soffice, even when available in this environment, is never reached)."""
    path = _write_dummy_docx(tmp_path)
    seen_backends: list[tuple[str, ...]] = []

    def _fake_check_render_capability(docx_path, *, backends, max_retries=1):
        seen_backends.append(tuple(b.name for b in backends))
        return {"status": fallbacks.RENDERED, "backend": backends[0].name}

    monkeypatch.setattr(fallbacks, "check_render_capability", _fake_check_render_capability)

    result = fallbacks.check_word_com_render_receipt(path)

    assert seen_backends == [("word-com",)]
    assert result["status"] == fallbacks.RENDERED
    assert result["backend"] == "word-com"


def test_word_com_backend_is_registered_with_the_delegate_functions():
    """check_render_capability's default KNOWN_BACKENDS chain must actually
    use the delegate functions (not some other implementation) -- a
    structural check that doesn't depend on mutating a frozen dataclass's
    already-bound callables."""
    assert fallbacks._WORD_COM_BACKEND.unavailable_reason is fallbacks._word_com_delegate_unavailable_reason
    assert fallbacks._WORD_COM_BACKEND.render is fallbacks._word_com_delegate_render
    assert fallbacks._SOFFICE_BACKEND.unavailable_reason is fallbacks._soffice_unavailable_reason
    assert fallbacks._SOFFICE_BACKEND.render is fallbacks._soffice_render
