"""Render-capability detection and explicit visual-QA status (93cd9798).

This module is capability DETECTION, not a rendering engine: meridian-docs is
stdlib-only for its core DOCX parsing (see ``docs_intel.py``'s module
docstring) and ``local_ingest.py`` deliberately does not embed a PDF/OOXML
rendering pipeline -- ``local_ingest.extract_text`` raises
``UnsupportedDocumentError`` for ``.pdf`` with a concrete reason string
("no PDF library installed") rather than pretending to have handled it. This
module applies the same philosophy to the layer above text extraction:
whether a DOCX can be visually rendered for QA at all.

Why this exists: :func:`docs_intel.read_document_snapshot` proves a document
can be *parsed* (paragraphs, headings, XML parts) -- it says nothing about
whether the document *renders* the way a human reviewer would see it in Word.
Before this module, there was no way for a caller to distinguish three very
different situations:

  1. We actually verified the document renders (a real backend produced
     visual output for it) -- trustworthy visual QA.
  2. We have no way to check in this environment (no render backend is
     installed/reachable) -- an environment limitation, not a statement
     about the document.
  3. We tried to render it and the attempt errored -- a real failure that
     must never be reported as "rendered" or silently folded into
     "unavailable".

:func:`check_render_capability` returns exactly one of three states --
``"rendered"``, ``"unavailable-with-reason"``, or ``"failed"`` -- so a caller
(including an LLM agent) can never mistake "we couldn't check" for "we
verified this renders", matching the read-only status-dict convention
``docs_intel.read_document_snapshot`` already uses (``{"error": ...}`` on
failure, a real payload on success) rather than that function's raise-on-
failure sibling functions.

Rendering itself is delegated to pluggable :class:`RenderBackend` probes
(LibreOffice ``soffice --headless --convert-to pdf`` and, on Windows, Word
COM automation via ``pywin32``) so this module stays a thin, mockable
detection layer -- exactly like ``local_ingest.py``'s PDF boundary defers to
the caller rather than shipping a bespoke PDF engine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Sequence

__all__ = [
    "RENDERED",
    "UNAVAILABLE_WITH_REASON",
    "FAILED",
    "RENDER_STATUSES",
    "RenderCapabilityError",
    "RenderBackend",
    "KNOWN_BACKENDS",
    "detect_backend",
    "check_render_capability",
]

# ---------------------------------------------------------------------------
# The three-state contract (non-negotiable -- see sprint item 93cd9798).
# ---------------------------------------------------------------------------

RENDERED = "rendered"
UNAVAILABLE_WITH_REASON = "unavailable-with-reason"
FAILED = "failed"

RENDER_STATUSES: tuple[str, str, str] = (RENDERED, UNAVAILABLE_WITH_REASON, FAILED)


class RenderCapabilityError(Exception):
    """Raised by a :class:`RenderBackend`'s ``render`` callable when a render
    attempt for a specific document fails.

    :func:`check_render_capability` catches this (and any other exception a
    backend raises) and converts it into a ``"failed"`` status -- it is never
    allowed to propagate to the caller and never silently reported as
    ``"rendered"``.
    """


@dataclass(frozen=True)
class RenderBackend:
    """A pluggable render-capability probe + renderer.

    ``unavailable_reason`` must be cheap (a ``shutil.which`` / ``find_spec``
    style lookup, never a slow subprocess render) -- this module does
    capability DETECTION, not rendering. It returns ``None`` when the backend
    is available, or a concrete, itemizable reason string when it is not.

    ``render`` is only invoked once ``unavailable_reason()`` has already
    returned ``None`` for this backend. It should raise
    :class:`RenderCapabilityError` (or let any other exception propagate --
    :func:`check_render_capability` wraps all of them uniformly) if the
    render attempt for ``docx_path`` fails.
    """

    name: str
    unavailable_reason: Callable[[], str | None]
    render: Callable[[str], dict[str, Any]]

    def is_available(self) -> bool:
        return self.unavailable_reason() is None


# ---------------------------------------------------------------------------
# Default backend: LibreOffice / OpenOffice headless conversion.
# ---------------------------------------------------------------------------

def _soffice_executable() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _soffice_unavailable_reason() -> str | None:
    if _soffice_executable() is not None:
        return None
    return "LibreOffice ('soffice'/'libreoffice') not found on PATH"


def _soffice_render(docx_path: str) -> dict[str, Any]:
    executable = _soffice_executable()
    if executable is None:
        # unavailable_reason() should have prevented this call; guard anyway
        # so a race (PATH changing mid-process) still fails loudly, not silently.
        raise RenderCapabilityError(
            "soffice executable disappeared between capability check and render"
        )
    with tempfile.TemporaryDirectory(prefix="meridian_render_gate_") as out_dir:
        try:
            result = subprocess.run(
                [executable, "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RenderCapabilityError(f"soffice conversion could not start: {exc}") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RenderCapabilityError(
                f"soffice --convert-to pdf exited with code {result.returncode}: "
                f"{stderr or '(no stderr output)'}"
            )
        produced = [name for name in os.listdir(out_dir) if name.lower().endswith(".pdf")]
        if not produced:
            raise RenderCapabilityError(
                "soffice reported success (exit code 0) but produced no .pdf output"
            )
        return {"converted_via": "soffice", "output_filename": produced[0]}


_SOFFICE_BACKEND = RenderBackend(
    name="libreoffice-soffice",
    unavailable_reason=_soffice_unavailable_reason,
    render=_soffice_render,
)


# ---------------------------------------------------------------------------
# Windows-only backend: Word COM automation via pywin32.
# ---------------------------------------------------------------------------

_WD_FORMAT_PDF = 17  # win32com.client.constants.wdFormatPDF


def _word_com_unavailable_reason() -> str | None:
    if sys.platform != "win32":
        return f"Word COM automation is only available on win32 (current platform: {sys.platform})"
    try:
        import win32com.client  # noqa: F401  (import-only availability probe)
    except ImportError:
        return "pywin32 (win32com) is not installed -- Word COM automation unavailable"
    return None


def _word_com_render(docx_path: str) -> dict[str, Any]:
    import win32com.client  # local import: optional dependency, only touched when available

    with tempfile.TemporaryDirectory(prefix="meridian_render_gate_") as out_dir:
        pdf_path = os.path.join(out_dir, "render_probe.pdf")
        word = None
        doc = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            doc = word.Documents.Open(os.path.abspath(docx_path), ReadOnly=True)
            doc.SaveAs(pdf_path, FileFormat=_WD_FORMAT_PDF)
        except Exception as exc:  # COM errors surface as broad pywintypes.com_error
            raise RenderCapabilityError(f"Word COM render failed: {exc}") from exc
        finally:
            if doc is not None:
                doc.Close(False)
            if word is not None:
                word.Quit()
        if not os.path.exists(pdf_path):
            raise RenderCapabilityError(
                "Word COM reported success but no PDF was written to disk"
            )
        return {"converted_via": "word-com", "output_filename": os.path.basename(pdf_path)}


_WORD_COM_BACKEND = RenderBackend(
    name="word-com",
    unavailable_reason=_word_com_unavailable_reason,
    render=_word_com_render,
)

KNOWN_BACKENDS: tuple[RenderBackend, ...] = (_SOFFICE_BACKEND, _WORD_COM_BACKEND)


# ---------------------------------------------------------------------------
# Detection + the public status check.
# ---------------------------------------------------------------------------

def detect_backend(
    backends: Sequence[RenderBackend] = KNOWN_BACKENDS,
) -> tuple[RenderBackend | None, list[str]]:
    """Return the first available backend, plus the itemized unavailability
    reasons collected from every backend that was checked.

    The reasons list is populated even when a backend IS found (it just won't
    contain the winning backend's reason, since it has none) so a caller who
    wants full diagnostics always has them; :func:`check_render_capability`
    uses it to build a real, itemized reason string when NO backend is
    available, rather than a generic "unavailable" message.
    """
    reasons: list[str] = []
    for backend in backends:
        reason = backend.unavailable_reason()
        if reason is None:
            return backend, reasons
        reasons.append(f"{backend.name}: {reason}")
    return None, reasons


def _result(
    status: str,
    *,
    reason: str | None = None,
    backend: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in RENDER_STATUSES:  # pragma: no cover -- internal invariant
        raise AssertionError(f"invalid render status: {status!r}")
    out: dict[str, Any] = {"status": status}
    if reason is not None:
        out["reason"] = reason
    if backend is not None:
        out["backend"] = backend
    if detail:
        out["detail"] = detail
    return out


def check_render_capability(
    docx_path: str,
    *,
    backends: Sequence[RenderBackend] = KNOWN_BACKENDS,
) -> dict[str, Any]:
    """Capability-detection status check for visual-QA rendering readiness.

    Returns a dict with a ``status`` key that is exactly one of:

      - ``"rendered"`` -- a render backend was available AND the render
        attempt for ``docx_path`` succeeded. This is the ONLY status that
        means "we verified this document renders". Includes ``backend`` (the
        backend name) and ``detail`` (backend-specific render info).

      - ``"unavailable-with-reason"`` -- no render backend is available in
        this environment. ``reason`` is a real, itemized explanation (every
        backend checked and why it was rejected), never a generic
        "unavailable" string. This means "we could not check" -- it says
        nothing about whether the document itself is valid.

      - ``"failed"`` -- a render backend WAS available but the render attempt
        for this specific document raised. ``reason`` carries the backend's
        error. A failure here is NEVER reported as ``"rendered"`` and is
        NEVER folded into ``"unavailable-with-reason"`` -- the two mean
        different things (environment can't check vs. we checked and it
        broke) and callers must be able to tell them apart.

    A missing/invalid ``docx_path`` is also reported as ``"failed"`` (a
    concrete, checkable error about this specific call), not
    ``"unavailable-with-reason"`` (which is reserved for environment-level
    capability gaps that are true regardless of which document was passed).
    """
    if not docx_path or not str(docx_path).strip():
        return _result(FAILED, reason="docx_path must be a non-empty string")
    if not os.path.exists(docx_path):
        return _result(FAILED, reason=f"no such file: {docx_path}")
    if not os.path.isfile(docx_path):
        return _result(FAILED, reason=f"not a file: {docx_path}")

    backend, reasons = detect_backend(backends)
    if backend is None:
        if not reasons:
            reasons = ["no render backends registered"]
        return _result(
            UNAVAILABLE_WITH_REASON,
            reason="no render backend available in this environment: " + "; ".join(reasons),
        )

    try:
        detail = backend.render(docx_path)
    except RenderCapabilityError as exc:
        return _result(FAILED, reason=str(exc), backend=backend.name)
    except Exception as exc:  # backend bug / unexpected subprocess or COM error
        return _result(FAILED, reason=f"{type(exc).__name__}: {exc}", backend=backend.name)

    return _result(RENDERED, backend=backend.name, detail=detail)
