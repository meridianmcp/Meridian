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

c7cc9da4 -- :func:`verify_promotion_readiness` builds on both halves of
that pairing (a fingerprinted read-only snapshot + this module's render
check) to gate a Meridian-docs REVIEW SESSION's draft/overlay promotion:
source-fingerprint equality against the snapshot's ``source_sha256``, a
cheap structural comparison, and this module's own three-state render
check, combined into one fail-closed readiness verdict that never mutates
either file -- the actual promotion still goes through
``docs_intel.merge_draft_into_canonical``.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import docs_intel

__all__ = [
    "RENDERED",
    "UNAVAILABLE_WITH_REASON",
    "FAILED",
    "RENDER_STATUSES",
    "TIMEOUT_ERROR",
    "TRANSPORT_ERROR",
    "CORRUPTION_ERROR",
    "UNKNOWN_ERROR",
    "FAILURE_CLASSES",
    "RenderCapabilityError",
    "RenderBackend",
    "KNOWN_BACKENDS",
    "detect_backend",
    "check_render_capability",
    "verify_promotion_readiness",
]

# ---------------------------------------------------------------------------
# The three-state contract (non-negotiable -- see sprint item 93cd9798).
# ---------------------------------------------------------------------------

RENDERED = "rendered"
UNAVAILABLE_WITH_REASON = "unavailable-with-reason"
FAILED = "failed"

RENDER_STATUSES: tuple[str, str, str] = (RENDERED, UNAVAILABLE_WITH_REASON, FAILED)


# ---------------------------------------------------------------------------
# c44d245d -- bounded, diagnostic failure classification for the ``"failed"``
# status. The three-state contract above (rendered / unavailable-with-reason /
# failed) stays exactly as-is -- this classifies *why* a ``"failed"`` result
# happened, carried as ``error_class`` on both the raised
# :class:`RenderCapabilityError` and the ``detail`` dict of the returned
# result, so a caller can tell "the render backend hung" apart from "the
# render backend couldn't even start" apart from "the document itself is
# broken" instead of collapsing all three into one opaque reason string.
# ---------------------------------------------------------------------------

TIMEOUT_ERROR = "timeout"
TRANSPORT_ERROR = "transport"
CORRUPTION_ERROR = "corruption"
UNKNOWN_ERROR = "unknown"

FAILURE_CLASSES: tuple[str, str, str, str] = (
    TIMEOUT_ERROR,
    TRANSPORT_ERROR,
    CORRUPTION_ERROR,
    UNKNOWN_ERROR,
)


class RenderCapabilityError(Exception):
    """Raised by a :class:`RenderBackend`'s ``render`` callable when a render
    attempt for a specific document fails.

    :func:`check_render_capability` catches this (and any other exception a
    backend raises) and converts it into a ``"failed"`` status -- it is never
    allowed to propagate to the caller and never silently reported as
    ``"rendered"``.

    c44d245d -- carries structured diagnostic fields so ``check_render_
    capability`` can report *why* a render failed, not just that it did:

    * ``error_class`` -- one of :data:`FAILURE_CLASSES`. Defaults to
      ``UNKNOWN_ERROR`` so existing ``raise RenderCapabilityError("...")``
      call sites (message-only) keep working unchanged.
    * ``exit_code`` -- the subprocess exit code, when the failure came from a
      subprocess backend that actually ran to completion.
    * ``stderr`` -- captured stderr text, when available.
    * ``timed_out`` -- ``True`` when the render attempt was killed for
      exceeding its bounded time budget.
    * ``retryable`` -- ``True`` only for failures that are safe AND likely
      useful to retry (idempotent transport hiccups: the backend couldn't be
      reached/spawned this one time). Timeouts and document-corruption
      failures are never retryable -- retrying either just repeats the same
      outcome at extra cost (corruption) or doubles the wait for no new
      information (timeout).
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: str = UNKNOWN_ERROR,
        exit_code: int | None = None,
        stderr: str | None = None,
        timed_out: bool = False,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_class = error_class if error_class in FAILURE_CLASSES else UNKNOWN_ERROR
        self.exit_code = exit_code
        self.stderr = stderr
        self.timed_out = timed_out
        self.retryable = bool(retryable)


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


# c44d245d -- module-level so tests can monkeypatch a short bound instead of
# waiting out a real 60s timeout to exercise the timeout-classification path.
_SOFFICE_TIMEOUT_SECONDS = 60.0

# Substrings (lowercased) in soffice's stderr that indicate the SOURCE
# document itself is the problem (a genuinely corrupt/unreadable .docx),
# as opposed to a transient environment hiccup (profile lock contention,
# a busy display server, a momentarily-unavailable temp dir, etc.). This is
# a best-effort heuristic, not a guarantee -- soffice does not have a
# machine-readable error-classification exit code, so this is the same kind
# of "read the diagnostic text" classification a human operator would do.
_SOFFICE_CORRUPTION_MARKERS = (
    "source file could not be loaded",
    "not a valid",
    "corrupt",
    "damaged",
    "unreadable content",
    "cannot be read",
)


def _classify_soffice_failure(stderr: str) -> tuple[str, bool]:
    """Return ``(error_class, retryable)`` for a nonzero-exit soffice run."""
    lowered = (stderr or "").lower()
    if any(marker in lowered for marker in _SOFFICE_CORRUPTION_MARKERS):
        return CORRUPTION_ERROR, False
    # No corruption marker found -- treat as a transient transport/environment
    # issue (e.g. soffice's user-profile lock held by another instance) and
    # allow ONE retry; check_render_capability enforces the actual bound.
    return TRANSPORT_ERROR, True


def _soffice_render(docx_path: str) -> dict[str, Any]:
    executable = _soffice_executable()
    if executable is None:
        # unavailable_reason() should have prevented this call; guard anyway
        # so a race (PATH changing mid-process) still fails loudly, not silently.
        raise RenderCapabilityError(
            "soffice executable disappeared between capability check and render",
            error_class=TRANSPORT_ERROR,
            retryable=True,
        )
    with tempfile.TemporaryDirectory(prefix="meridian_render_gate_") as out_dir:
        try:
            result = subprocess.run(
                [executable, "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
                capture_output=True,
                timeout=_SOFFICE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run() already kills (process.kill()) the EXACT child
            # process IT spawned before re-raising TimeoutExpired -- this
            # never touches any other soffice instance running on the
            # machine, satisfying "clean only Meridian-owned processes"
            # without any extra process-sweeping logic. Timeouts are never
            # retried: a render that hung once is likely to hang again, and
            # retrying just doubles the wait for no new information.
            stderr = None
            if exc.stderr:
                stderr = (
                    exc.stderr.decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, (bytes, bytearray))
                    else str(exc.stderr)
                )
            raise RenderCapabilityError(
                f"soffice --convert-to pdf exceeded its {_SOFFICE_TIMEOUT_SECONDS:.0f}s "
                "bound and was terminated",
                error_class=TIMEOUT_ERROR,
                timed_out=True,
                stderr=stderr,
                retryable=False,
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RenderCapabilityError(
                f"soffice conversion could not start: {exc}",
                error_class=TRANSPORT_ERROR,
                retryable=True,
            ) from exc
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            error_class, retryable = _classify_soffice_failure(stderr)
            raise RenderCapabilityError(
                f"soffice --convert-to pdf exited with code {result.returncode}: "
                f"{stderr or '(no stderr output)'}",
                error_class=error_class,
                exit_code=result.returncode,
                stderr=stderr or None,
                retryable=retryable,
            )
        produced = [name for name in os.listdir(out_dir) if name.lower().endswith(".pdf")]
        if not produced:
            raise RenderCapabilityError(
                "soffice reported success (exit code 0) but produced no .pdf output",
                error_class=UNKNOWN_ERROR,
                exit_code=result.returncode,
                retryable=False,
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


# c44d245d -- module-level so tests can shrink the bound instead of waiting
# out a real 60s hang to exercise the timeout-classification/cleanup path.
_WORD_COM_TIMEOUT_SECONDS = 60.0

# Word COM errors surface as a broad pywintypes.com_error whose message text
# is the only signal available (no structured error code Python can reliably
# decode across Office versions) -- same best-effort text-classification
# philosophy as _classify_soffice_failure above.
_WORD_COM_TRANSIENT_MARKERS = (
    "rpc server is unavailable",
    "call was rejected by callee",
    "server execution failed",
    "operation unavailable",
    "the message filter indicated",
)
_WORD_COM_CORRUPTION_MARKERS = (
    "cannot open",
    "is not a valid",
    "cannot be opened because there are problems",
    "converter",
    "damaged",
)


def _classify_word_com_exception(exc: BaseException) -> tuple[str, bool]:
    """Return ``(error_class, retryable)`` for an exception raised while
    driving Word COM automation."""
    message = str(exc).lower()
    if any(marker in message for marker in _WORD_COM_TRANSIENT_MARKERS):
        return TRANSPORT_ERROR, True
    if any(marker in message for marker in _WORD_COM_CORRUPTION_MARKERS):
        return CORRUPTION_ERROR, False
    return UNKNOWN_ERROR, False


def _word_application_pid(word: Any) -> int | None:
    """Best-effort resolve the OS process id backing a ``Word.Application``
    COM object, via its main window handle. Returns ``None`` (never raises)
    when pywin32's ``win32process`` isn't importable or the handle can't be
    resolved -- callers must treat a ``None`` pid as "cleanup unavailable",
    not as an error.
    """
    try:
        import win32process  # local import: optional dependency, pywin32-only

        hwnd = word.Hwnd
        _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
        return int(process_id)
    except Exception:
        return None


def _terminate_owned_process(pid: int) -> bool:
    """Best-effort terminate exactly the process id THIS call spawned and
    tracked (never a sweep of every WINWORD.EXE on the machine) after a
    bounded Word COM render exceeded its time budget. Returns ``True`` on a
    best-effort success, ``False`` on any failure -- never raises, since this
    already runs on the "something went badly wrong" path and must not mask
    the real timeout with a secondary crash.
    """
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _word_com_render(docx_path: str) -> dict[str, Any]:
    import win32com.client  # local import: optional dependency, only touched when available

    with tempfile.TemporaryDirectory(prefix="meridian_render_gate_") as out_dir:
        pdf_path = os.path.join(out_dir, "render_probe.pdf")
        outcome: dict[str, Any] = {}
        owned: dict[str, int | None] = {"pid": None}

        def _worker() -> None:
            word = None
            doc = None
            try:
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                owned["pid"] = _word_application_pid(word)
                doc = word.Documents.Open(os.path.abspath(docx_path), ReadOnly=True)
                doc.SaveAs(pdf_path, FileFormat=_WD_FORMAT_PDF)
            except Exception as exc:  # COM errors surface as broad pywintypes.com_error
                outcome["exc"] = exc
            finally:
                try:
                    if doc is not None:
                        doc.Close(False)
                except Exception:
                    pass
                try:
                    if word is not None:
                        word.Quit()
                except Exception:
                    pass

        # c44d245d -- Word COM automation has no native call-level timeout
        # (a modal "keep changes?"/repair prompt can block Documents.Open
        # indefinitely). Bound it explicitly with a watchdog thread: if the
        # worker hasn't finished within the budget, terminate ONLY the exact
        # Word process this call spawned (never every WINWORD.EXE running on
        # the box) and report a classified timeout rather than hanging the
        # whole render-capability check forever.
        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()
        worker_thread.join(_WORD_COM_TIMEOUT_SECONDS)

        if worker_thread.is_alive():
            pid = owned.get("pid")
            terminated = _terminate_owned_process(pid) if pid is not None else False
            raise RenderCapabilityError(
                f"Word COM render exceeded its {_WORD_COM_TIMEOUT_SECONDS:.0f}s bound "
                f"and was terminated (owned pid={pid!r}, terminated={terminated})",
                error_class=TIMEOUT_ERROR,
                timed_out=True,
                retryable=False,
            )

        if "exc" in outcome:
            exc = outcome["exc"]
            error_class, retryable = _classify_word_com_exception(exc)
            raise RenderCapabilityError(
                f"Word COM render failed: {exc}",
                error_class=error_class,
                retryable=retryable,
            ) from exc

        if not os.path.exists(pdf_path):
            raise RenderCapabilityError(
                "Word COM reported success but no PDF was written to disk",
                error_class=UNKNOWN_ERROR,
                retryable=False,
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


def _failure_detail(exc: RenderCapabilityError | None, *, attempts: int, exception_type: str | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {"attempts": attempts}
    if exc is not None:
        detail["error_class"] = exc.error_class
        detail["timed_out"] = exc.timed_out
        if exc.exit_code is not None:
            detail["exit_code"] = exc.exit_code
        if exc.stderr:
            detail["stderr"] = exc.stderr
    else:
        detail["error_class"] = UNKNOWN_ERROR
        detail["timed_out"] = False
    if exception_type is not None:
        detail["exception_type"] = exception_type
    return detail


def check_render_capability(
    docx_path: str,
    *,
    backends: Sequence[RenderBackend] = KNOWN_BACKENDS,
    max_retries: int = 1,
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

    c44d245d -- bounded, diagnostic failure handling on top of the three-state
    contract above (which is unchanged):

      - A ``"failed"`` result's ``detail`` now always carries ``error_class``
        (one of :data:`FAILURE_CLASSES`: ``"timeout"``, ``"transport"``,
        ``"corruption"``, ``"unknown"``), ``timed_out``, and ``attempts``, plus
        ``exit_code``/``stderr`` when the backend captured them. This lets a
        caller distinguish "the backend hung", "the backend couldn't be
        reached/spawned", and "this specific document is broken" instead of
        parsing a free-text reason string.
      - ``max_retries`` (default 1) bounds automatic retry of a render
        attempt, and ONLY when the immediately-preceding failure classified
        itself as ``retryable=True`` (a transient, idempotent transport
        hiccup -- rendering never mutates ``docx_path``, so retrying is always
        safe from a data standpoint; the classification is about whether
        it's *useful*, not just safe). Timeouts and document-corruption
        failures are never retryable and so are never retried, no matter how
        high ``max_retries`` is set.
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

    attempts = 0
    while True:
        attempts += 1
        try:
            detail = backend.render(docx_path)
        except RenderCapabilityError as exc:
            if exc.retryable and attempts <= max_retries:
                continue
            return _result(
                FAILED,
                reason=str(exc),
                backend=backend.name,
                detail=_failure_detail(exc, attempts=attempts),
            )
        except Exception as exc:  # backend bug / unexpected subprocess or COM error
            # An unclassified exception (not RenderCapabilityError) is never
            # retried -- only a backend that explicitly classifies its own
            # failure as retryable gets the retry budget.
            return _result(
                FAILED,
                reason=f"{type(exc).__name__}: {exc}",
                backend=backend.name,
                detail=_failure_detail(None, attempts=attempts, exception_type=type(exc).__name__),
            )
        else:
            return _result(RENDERED, backend=backend.name, detail=detail)


# ---------------------------------------------------------------------------
# Promotion gate: fingerprint equality + structural/render verification
# (c7cc9da4) for a Meridian-docs review session's draft/overlay.
# ---------------------------------------------------------------------------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_file_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _docx_media_count(raw: bytes) -> int:
    """Count ``word/media/*`` parts in the ZIP bytes.

    Deliberately self-contained (does not reach into
    ``docs_intel``'s private ``_docx_media_count``) so this module keeps its
    own minimal, independently-readable structural check -- the same
    stdlib-only philosophy the rest of this module already follows.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return sum(1 for name in archive.namelist() if name.startswith("word/media/"))
    except zipfile.BadZipFile:
        return 0


def _docx_table_count(raw: bytes) -> int:
    """Count top-level ``<w:tbl>`` elements in ``word/document.xml``."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile):
        return 0
    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError:
        return 0
    return sum(1 for _ in root.iter(f"{{{_W_NS}}}tbl"))


def _structural_snapshot(raw: bytes) -> dict[str, int | None]:
    """A cheap, read-only structural fingerprint: paragraph/media/table
    counts. Reuses ``docs_intel.parse_docx`` (public API) for the paragraph
    count rather than duplicating OOXML paragraph-walking logic here.
    """
    try:
        paragraph_count: int | None = len(docs_intel.parse_docx(raw))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        paragraph_count = None
    return {
        "paragraph_count": paragraph_count,
        "media_count": _docx_media_count(raw),
        "table_count": _docx_table_count(raw),
    }


def verify_promotion_readiness(
    canonical_path: str,
    draft_path: str,
    expected_source_sha256: str,
    *,
    require_render: bool = False,
    backends: Sequence[RenderBackend] = KNOWN_BACKENDS,
) -> dict[str, Any]:
    """Fail-closed pre-promotion gate for a Meridian-docs review-session draft.

    A review session reads a read-only snapshot of ``canonical_path`` (via
    ``docs_intel.read_document_snapshot``, which stamps a ``source_sha256``
    fingerprint of the exact bytes it read), accumulates recommendations
    anchored against that snapshot, and stages any accepted edits into an
    ISOLATED draft/overlay -- never ``canonical_path`` itself (see
    ``move_section`` / ``copy_section`` / ``relocate_figure`` /
    ``relocate_table``'s ``draft_output_path`` parameter). Before that draft
    is handed to ``merge_docx_draft`` (``docs_intel.merge_draft_into_
    canonical``) for the actual promotion, THIS function is the check a
    caller must never skip:

      1. SOURCE-FINGERPRINT EQUALITY -- ``canonical_path`` is re-read and
         re-fingerprinted exactly as it sits on disk RIGHT NOW and compared
         against ``expected_source_sha256`` (the ``source_sha256`` the
         review session's original ``read_document_snapshot`` call
         recorded). A mismatch means ``canonical_path`` was saved -- in
         Word, or by a different writer -- since the review snapshot was
         taken, so every recommendation's anchor may now point at stale
         content: this is refused (``ready=False``), never silently waved
         through.
      2. STRUCTURAL VERIFICATION -- a cheap, read-only comparison of
         paragraph/media/table counts between ``draft_path`` and
         ``canonical_path``. ``draft_path`` must parse as a valid .docx and
         must never have FEWER media or table elements than
         ``canonical_path`` (it may add; it must never silently drop) --
         the same "never silently lose structural content" invariant
         ``docs_intel._atomic_write_docx_bytes`` already enforces at write
         time, applied here as an early, pre-flight sanity check rather
         than a duplicate of that write-time gate (``merge_docx_draft``
         still performs its own, more thorough structural verification at
         promotion time; this is a cheap first filter, not a replacement).
      3. RENDER VERIFICATION -- ``check_render_capability`` is run against
         ``draft_path`` (the artifact about to become canonical) and its
         three-state result is always returned under ``render_check``. In
         keeping with this module's core contract -- "we could not check"
         must never be conflated with "we verified" OR treated as a
         document defect -- an environment with no render backend does NOT
         fail this gate by default (``require_render=False``): the
         "unavailable-with-reason" result is surfaced as a caveat, not a
         blocker. Pass ``require_render=True`` for a caller/project that
         has declared rendering a REQUIRED capability (mirroring the
         required/optional/degraded_ok distinction Meridian's capability
         manifests already use elsewhere) -- with it set, anything other
         than a real ``"rendered"`` result blocks promotion.

    This function NEVER mutates ``canonical_path`` or ``draft_path`` -- it
    is pure read-only verification, fail-closed by construction (any
    missing argument, unreadable file, fingerprint mismatch, or dropped
    structural family makes ``ready=False``). The actual promotion
    (staging, atomic swap, restore-on-failure) remains
    ``merge_docx_draft``'s job -- this only decides whether that call
    should be attempted at all.

    Returns ``{"ready": bool, "reason": str | None, "fingerprint_check":
    {...}, "structural_check": {...}, "render_check": {...} | None}``.
    ``reason`` is ``None`` exactly when ``ready`` is ``True``; otherwise it
    is a semicolon-joined summary of every check that failed (not just the
    first one), so a caller sees the whole picture in one call.
    """
    if not canonical_path or not str(canonical_path).strip():
        return {"ready": False, "reason": "canonical_path must be a non-empty string"}
    if not draft_path or not str(draft_path).strip():
        return {"ready": False, "reason": "draft_path must be a non-empty string"}
    if not expected_source_sha256 or not str(expected_source_sha256).strip():
        return {
            "ready": False,
            "reason": (
                "expected_source_sha256 must be a non-empty string -- pass "
                "the source_sha256 a prior docs_intel.read_document_snapshot "
                "call recorded for canonical_path before this review "
                "session's recommendations were built"
            ),
        }

    reasons: list[str] = []

    canonical_raw = _read_file_bytes(canonical_path)
    if canonical_raw is None:
        return {
            "ready": False,
            "reason": f"could not read canonical_path: {canonical_path!r}",
        }
    current_source_sha256 = _sha256_bytes(canonical_raw)
    fingerprint_match = current_source_sha256 == expected_source_sha256
    fingerprint_check: dict[str, Any] = {
        "expected_source_sha256": expected_source_sha256,
        "current_source_sha256": current_source_sha256,
        "match": fingerprint_match,
    }
    if not fingerprint_match:
        reasons.append(
            "canonical_path has changed on disk since the review snapshot's "
            "source_sha256 was recorded -- every recommendation's anchor "
            "may now point at stale content; refusing to promote"
        )

    draft_raw = _read_file_bytes(draft_path)
    structural_check: dict[str, Any]
    if draft_raw is None:
        structural_check = {"error": f"could not read draft_path: {draft_path!r}"}
        reasons.append(structural_check["error"])
    else:
        canonical_structure = _structural_snapshot(canonical_raw)
        draft_structure = _structural_snapshot(draft_raw)
        dropped = [
            key
            for key in ("media_count", "table_count")
            if draft_structure[key] is not None
            and canonical_structure[key] is not None
            and draft_structure[key] < canonical_structure[key]
        ]
        structural_check = {
            "canonical": canonical_structure,
            "draft": draft_structure,
            "dropped_families": dropped,
        }
        if draft_structure["paragraph_count"] is None:
            reasons.append(f"draft_path {draft_path!r} is not a parseable .docx")
        if dropped:
            reasons.append(
                "draft_path has FEWER " + ", ".join(dropped) + " than "
                "canonical_path -- a draft must never silently drop "
                "structural content relative to what it is replacing"
            )

    render_check: dict[str, Any] | None = None
    if draft_raw is not None:
        render_check = check_render_capability(draft_path, backends=backends)
        if require_render and render_check["status"] != RENDERED:
            reasons.append(
                "require_render=True and draft_path did not verify as "
                f"rendered (status={render_check['status']!r}): "
                f"{render_check.get('reason', '(no reason given)')}"
            )

    ready = not reasons
    return {
        "ready": ready,
        "reason": None if ready else "; ".join(reasons),
        "fingerprint_check": fingerprint_check,
        "structural_check": structural_check,
        "render_check": render_check,
    }
