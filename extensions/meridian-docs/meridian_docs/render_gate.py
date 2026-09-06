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

1e6150ef (MDE-7 P1) -- durable render receipts with visual-QA evidence
------------------------------------------------------------------------
Everything above this section answers "can/did this document render right
now" as a single in-memory dict -- useful for one call, but gone the moment
the render backend's own ``tempfile.TemporaryDirectory()`` is cleaned up,
and silent about several things a release decision actually needs: WHICH
backend, in what configured priority order; WHAT the backend's own version
was; the render's process identity (so an orphaned Word process is
traceable, not just prevented); the produced PDF's own hash/page count
(computed while the PDF still exists, so a receipt survives the temp
cleanup that destroys the file itself); whether the source document even
has a chance of rendering CURRENT content (``word/settings.xml``'s
``<w:updateFields>`` field-refresh flag -- a document whose fields
(TOC/page numbers/cross-references) won't auto-update on open can render
"successfully" while showing stale text); and, critically, whether backend
conversion success has been mistaken for an actual human/automated VISUAL
QA pass, which it never implies on its own.

:func:`render_with_receipt` wraps :func:`check_render_capability` and
builds a durable :class:`RenderReceipt` -- persisted (atomic JSON ledger,
same ``os.replace`` idiom used throughout this codebase's other sidecar
stores) to a caller-supplied ``receipts_path`` when given, so the receipt
outlives the render backend's temp directory. Its ``visual_qa`` field
defaults to an explicit ``"not_reviewed"`` state whenever a caller doesn't
pass a real visual-QA verdict -- render (backend conversion) success is
NEVER, by itself, recorded as visual verification.

:func:`check_release_render_gate` is the release-time enforcement point:
release is refused unless a FRESH (content-hash-matched, non-stale, real
``"rendered"``) receipt is on file, UNLESS a human explicitly passes
``allow_degraded_override=True`` with a non-empty ``override_reason`` --
which itself becomes a second, durable, audited receipt in the same
ledger, never a silent bypass. Per this module's own c44d245d contract, a
timed-out backend probe already reports as ``"failed"`` (never
``"rendered"``); :func:`render_with_receipt` restates that invariant
defensively for the receipt specifically, so "a timed-out COM probe is
failed/degraded evidence, never success" can never regress even if a
future change to :func:`check_render_capability` weakens it upstream.
"""
from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from . import docs_intel

__all__ = [
    "RENDERED",
    "UNAVAILABLE_WITH_REASON",
    "FAILED",
    "RENDER_STATUSES",
    "RENDER_TEMPDIR_PREFIX",
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
    "check_word_com_render_receipt",
    "verify_promotion_readiness",
    "verify_execution_manifest_promotion_readiness",
    "RenderReceipt",
    "render_with_receipt",
    "list_render_receipts",
    "check_release_render_gate",
]

# ---------------------------------------------------------------------------
# The three-state contract (non-negotiable -- see sprint item 93cd9798).
# ---------------------------------------------------------------------------

RENDERED = "rendered"
UNAVAILABLE_WITH_REASON = "unavailable-with-reason"
FAILED = "failed"

RENDER_STATUSES: tuple[str, str, str] = (RENDERED, UNAVAILABLE_WITH_REASON, FAILED)

# c7ef8ff7 -- named so it's a single source of truth for the three backend
# render() functions below (previously three independent string literals
# that could silently drift apart). This is also the documented STRING
# CONVENTION ``meridian.local_resilience.reap_stale_render_tempdirs``'s own
# ``prefix`` default matches -- kept in sync by convention/documentation,
# never by import: this package is standalone (no dependency on the
# ``meridian`` core package -- see this module's own docstring), so core's
# crash-recovery reaper for these exact disposable directories cannot
# import this constant and must instead default to the identical literal.
# A ``TemporaryDirectory`` normally self-cleans even on an in-process
# exception (each backend's own ``with`` block), but NOT if the whole host
# process is killed before that finalizer runs -- that crash-recovery case
# is what the reaper on the core side exists for.
RENDER_TEMPDIR_PREFIX = "meridian_render_gate_"


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
    with tempfile.TemporaryDirectory(prefix=RENDER_TEMPDIR_PREFIX) as out_dir:
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
        # 1e6150ef -- capture receipt fields (hash/size/page count) from the
        # produced PDF WHILE it still exists inside this TemporaryDirectory,
        # so a caller building a durable receipt from this return value never
        # needs the file to still be on disk afterward. ``pid`` is always
        # None here: subprocess.run() (used above, not Popen) does not retain
        # an inspectable child pid after it returns -- its own internal
        # timeout handling already guarantees no orphaned process regardless
        # (see the TimeoutExpired branch above), so this is a documented,
        # deliberate gap, not an oversight.
        receipt_fields = _pdf_receipt_fields(os.path.join(out_dir, produced[0]))
        return {
            "converted_via": "soffice", "output_filename": produced[0], "pid": None,
            **receipt_fields,
        }


_SOFFICE_BACKEND = RenderBackend(
    name="libreoffice-soffice",
    unavailable_reason=_soffice_unavailable_reason,
    render=_soffice_render,
)


def _soffice_version() -> str | None:
    """Best-effort ``soffice --version`` text, fetched as an independent,
    separately-mockable step (deliberately NOT called from inside
    :func:`_soffice_render` itself, so it can never interfere with that
    function's own ``subprocess.run`` call/timeout/retry contract). Returns
    ``None`` (never raises) when soffice isn't on PATH or the version probe
    itself fails for any reason -- a missing version string is a normal,
    reportable receipt gap, not an error.
    """
    executable = _soffice_executable()
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, timeout=10.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return text or None


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
# Give the COM worker a short grace period after its owned process is
# terminated.  The thread is deliberately never allowed to hold up the
# caller indefinitely: Word can block inside an overlapped COM call even after
# the process has been signalled.
_WORD_COM_CLEANUP_JOIN_SECONDS = 1.0

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


def _word_com_render_thread(docx_path: str) -> dict[str, Any]:
    import win32com.client  # local import: optional dependency, only touched when available

    with tempfile.TemporaryDirectory(prefix=RENDER_TEMPDIR_PREFIX) as out_dir:
        pdf_path = os.path.join(out_dir, "render_probe.pdf")
        outcome: dict[str, Any] = {}
        owned: dict[str, Any] = {"pid": None, "version": None}
        timeout_requested = threading.Event()

        def _worker() -> None:
            word = None
            doc = None
            try:
                # COM apartments are thread-local.  The watchdog worker is a
                # new thread, so relying on the host thread's initialization
                # is incorrect and can make Documents.Open hang or fail
                # nondeterministically on Windows.
                import pythoncom

                pythoncom.CoInitialize()
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                # Prevent modal prompts from turning a bounded render into an
                # unbounded worker wait.
                word.DisplayAlerts = 0  # wdAlertsNone
                owned["pid"] = _word_application_pid(word)
                # 1e6150ef -- best-effort process/version identity for the
                # durable render receipt. getattr(..., default) never raises
                # even against a fake/stub Word.Application in tests.
                owned["version"] = getattr(word, "Version", None)
                doc = word.Documents.Open(
                    os.path.abspath(docx_path),
                    ConfirmConversions=False,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    Revert=False,
                    OpenAndRepair=False,
                    NoEncodingDialog=True,
                )
                doc.SaveAs(pdf_path, FileFormat=_WD_FORMAT_PDF)
            except Exception as exc:  # COM errors surface as broad pywintypes.com_error
                outcome["exc"] = exc
            finally:
                # Once the watchdog has terminated Word, calling back into
                # the invalid COM proxy can raise an uncatchable Windows RPC
                # fault (0x800706BE). The worker thread is about to exit, so
                # let the OS tear down that apartment instead of attempting
                # cleanup against a dead server.
                if not timeout_requested.is_set():
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
                # Do not call CoUninitialize here. Word may already have
                # exited (or been terminated by the watchdog) while COM is
                # unwinding; on Windows that call can raise an uncatchable
                # RPC fault. This worker is short-lived and its thread exit
                # releases the apartment safely.

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
            timeout_requested.set()
            pid = owned.get("pid")
            terminated = _terminate_owned_process(pid) if pid is not None else False
            worker_thread.join(_WORD_COM_CLEANUP_JOIN_SECONDS)
            raise RenderCapabilityError(
                f"Word COM render exceeded its {_WORD_COM_TIMEOUT_SECONDS:.0f}s bound "
                f"and was terminated (owned pid={pid!r}, terminated={terminated}, "
                f"cleanup_pending={worker_thread.is_alive()})",
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
        receipt_fields = _pdf_receipt_fields(pdf_path)
        return {
            "converted_via": "word-com", "output_filename": os.path.basename(pdf_path),
            "pid": owned.get("pid"), "backend_version": owned.get("version"),
            **receipt_fields,
        }


def _word_com_process_worker(docx_path: str, pdf_path: str, result_queue: Any) -> None:
    """Run real Word COM in a killable child process.

    A blocked COM call can raise a Windows RPC fault outside Python's
    exception machinery when it runs in a thread inside the test/server
    process. Keeping the automation in a spawned child makes the timeout
    boundary real: the parent can terminate the child without taking down
    the MCP server or pytest interpreter.
    """
    word = None
    doc = None
    try:
        import pythoncom

        pythoncom.CoInitialize()
        import win32com.client

        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0  # wdAlertsNone
        result_queue.put({"kind": "pid", "pid": _word_application_pid(word)})
        result_queue.put({"kind": "version", "version": getattr(word, "Version", None)})
        doc = word.Documents.Open(
            os.path.abspath(docx_path),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Revert=False,
            OpenAndRepair=False,
            NoEncodingDialog=True,
        )
        doc.SaveAs(pdf_path, FileFormat=_WD_FORMAT_PDF)
        result_queue.put({"kind": "result", "ok": True})
    except BaseException as exc:  # child must report all failures to parent
        try:
            result_queue.put({
                "kind": "result",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            pass
    finally:
        # Do not call CoUninitialize in a process whose Word server may have
        # already gone away. Process exit releases this apartment safely.
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


def _word_com_render_isolated(docx_path: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=RENDER_TEMPDIR_PREFIX) as out_dir:
        pdf_path = os.path.join(out_dir, "render_probe.pdf")
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        worker = context.Process(
            target=_word_com_process_worker,
            args=(os.path.abspath(docx_path), pdf_path, result_queue),
        )
        worker.start()
        word_pid: int | None = None
        word_version: Any = None
        result: dict[str, Any] | None = None
        deadline = time.monotonic() + _WORD_COM_TIMEOUT_SECONDS
        try:
            while worker.is_alive() and time.monotonic() < deadline:
                try:
                    message = result_queue.get(
                        timeout=min(0.1, max(0.01, deadline - time.monotonic())),
                    )
                except queue.Empty:
                    continue
                if message.get("kind") == "pid":
                    word_pid = message.get("pid")
                elif message.get("kind") == "version":
                    word_version = message.get("version")
                elif message.get("kind") == "result":
                    result = message

            if worker.is_alive():
                terminated_word = (
                    _terminate_owned_process(word_pid)
                    if word_pid is not None else False
                )
                worker.terminate()
                worker.join(_WORD_COM_CLEANUP_JOIN_SECONDS)
                if worker.is_alive() and hasattr(worker, "kill"):
                    worker.kill()
                    worker.join(_WORD_COM_CLEANUP_JOIN_SECONDS)
                raise RenderCapabilityError(
                    f"Word COM render exceeded its {_WORD_COM_TIMEOUT_SECONDS:.0f}s "
                    f"bound (worker_pid={worker.pid!r}, owned_word_pid={word_pid!r}, "
                    f"word_terminated={terminated_word}, "
                    f"worker_alive={worker.is_alive()})",
                    error_class=TIMEOUT_ERROR,
                    timed_out=True,
                    retryable=False,
                )

            worker.join(_WORD_COM_CLEANUP_JOIN_SECONDS)
            # A result can still be in the feeder pipe immediately after the
            # child exits; drain briefly before treating it as a crash.
            while True:
                try:
                    message = result_queue.get(timeout=0.1)
                except queue.Empty:
                    break
                if message.get("kind") == "pid":
                    word_pid = message.get("pid")
                elif message.get("kind") == "version":
                    word_version = message.get("version")
                elif message.get("kind") == "result":
                    result = message
        finally:
            result_queue.close()
            result_queue.join_thread()

        if not result or not result.get("ok"):
            error = RuntimeError(
                (result or {}).get("error", f"worker exited with code {worker.exitcode}"),
            )
            error_class, retryable = _classify_word_com_exception(error)
            raise RenderCapabilityError(
                f"Word COM render failed: {error}",
                error_class=error_class,
                retryable=retryable,
            ) from error
        if not os.path.exists(pdf_path):
            raise RenderCapabilityError(
                "Word COM reported success but no PDF was written to disk",
                error_class=UNKNOWN_ERROR,
                retryable=False,
            )
        receipt_fields = _pdf_receipt_fields(pdf_path)
        return {
            "converted_via": "word-com", "output_filename": os.path.basename(pdf_path),
            "pid": word_pid, "backend_version": word_version,
            **receipt_fields,
        }


def _word_com_render(docx_path: str) -> dict[str, Any]:
    """Render with a real child process; keep injected fakes in-process."""
    import win32com.client

    if getattr(win32com.client, "__file__", None):
        return _word_com_render_isolated(docx_path)
    return _word_com_render_thread(docx_path)


_WORD_COM_BACKEND = RenderBackend(
    name="word-com",
    unavailable_reason=_word_com_unavailable_reason,
    render=_word_com_render,
)

KNOWN_BACKENDS: tuple[RenderBackend, ...] = (_SOFFICE_BACKEND, _WORD_COM_BACKEND)


# ---------------------------------------------------------------------------
# Word/COM-only render receipt (8419f55f).
#
# tools/meridian_fallbacks/docx_completion_gate.py's local DOCX completion
# gate accepts ONLY a real Word COM render as an external verification
# receipt: a LibreOffice/soffice conversion is a legitimate render-
# CAPABILITY signal for check_render_capability's general three-state
# contract above, but it is not what that gate's stricter contract means by
# "Word rendered this document" -- Word COM automation is the only backend
# that actually opens the file in the real authoring application the
# document is meant for. This helper scopes check_render_capability to just
# that one backend so callers needing this narrower guarantee don't have to
# reach into KNOWN_BACKENDS / the private _WORD_COM_BACKEND name themselves.
# ---------------------------------------------------------------------------

def check_word_com_render_receipt(docx_path: str) -> dict[str, Any]:
    """Like :func:`check_render_capability`, but restricted to the Word COM
    backend only (never LibreOffice/soffice).

    Returns the SAME three-state contract (``"rendered"`` /
    ``"unavailable-with-reason"`` / ``"failed"``), scoped to just the
    ``"word-com"`` backend: on any non-Windows platform, or a Windows
    machine without ``pywin32`` / Word installed, this returns
    ``"unavailable-with-reason"`` -- never ``"rendered"``.
    """
    return check_render_capability(docx_path, backends=(_WORD_COM_BACKEND,))


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

    1e6150ef -- every returned dict (all three statuses, including the
    early-validation ``"failed"`` results) also carries ``backend_order``:
    the full, ordered list of backend NAMES this call was configured with
    (``[b.name for b in backends]``) -- backend priority is already
    configurable via the ``backends`` parameter; this makes what order was
    actually IN EFFECT for this call part of the recorded result, not
    something a caller has to remember or re-derive. A ``"rendered"``
    result's ``detail`` also always carries ``attempts`` (how many render
    attempts, including retries, it took to succeed) -- previously only
    present on a ``"failed"`` result's detail.
    """
    backend_order = [b.name for b in backends]

    def _tag(result: dict[str, Any]) -> dict[str, Any]:
        result["backend_order"] = backend_order
        return result

    if not docx_path or not str(docx_path).strip():
        return _tag(_result(FAILED, reason="docx_path must be a non-empty string"))
    if not os.path.exists(docx_path):
        return _tag(_result(FAILED, reason=f"no such file: {docx_path}"))
    if not os.path.isfile(docx_path):
        return _tag(_result(FAILED, reason=f"not a file: {docx_path}"))

    backend, reasons = detect_backend(backends)
    if backend is None:
        if not reasons:
            reasons = ["no render backends registered"]
        return _tag(_result(
            UNAVAILABLE_WITH_REASON,
            reason="no render backend available in this environment: " + "; ".join(reasons),
        ))

    attempts = 0
    while True:
        attempts += 1
        try:
            detail = backend.render(docx_path)
        except RenderCapabilityError as exc:
            if exc.retryable and attempts <= max_retries:
                continue
            return _tag(_result(
                FAILED,
                reason=str(exc),
                backend=backend.name,
                detail=_failure_detail(exc, attempts=attempts),
            ))
        except Exception as exc:  # backend bug / unexpected subprocess or COM error
            # An unclassified exception (not RenderCapabilityError) is never
            # retried -- only a backend that explicitly classifies its own
            # failure as retryable gets the retry budget.
            return _tag(_result(
                FAILED,
                reason=f"{type(exc).__name__}: {exc}",
                backend=backend.name,
                detail=_failure_detail(None, attempts=attempts, exception_type=type(exc).__name__),
            ))
        else:
            return _tag(_result(RENDERED, backend=backend.name, detail={**detail, "attempts": attempts}))


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


# ---------------------------------------------------------------------------
# 1e6150ef -- durable render receipts with visual-QA evidence.
# ---------------------------------------------------------------------------

# Best-effort PDF page-count scan: counts ``/Type /Page`` object markers
# (never ``/Type /Pages``, the tree-container object). This is a lightweight
# byte-level scan, not a real PDF parse -- matches this module's stdlib-only,
# no-embedded-rendering-pipeline philosophy (see the module docstring). It
# under-counts for a PDF using compressed cross-reference/object streams;
# treat a returned page_count as a useful signal, not a guaranteed-exact one.
_PDF_PAGE_MARKER = re.compile(rb"/Type\s*/Page(?!s)\b")


def _pdf_receipt_fields(pdf_path: str) -> dict[str, Any]:
    """Best-effort receipt fields computed from a produced PDF's bytes WHILE
    they still exist -- this is what lets a receipt survive after the
    backend's own ``tempfile.TemporaryDirectory()`` (and the PDF inside it)
    is cleaned up. Never raises: an unreadable ``pdf_path`` yields all-``None``
    fields. ``page_count`` is ``None`` (never ``0``) when nothing could be
    counted, since ``0`` is not a trustworthy answer for "could not determine".
    """
    raw = _read_file_bytes(pdf_path)
    if raw is None:
        return {"pdf_sha256": None, "pdf_size_bytes": None, "page_count": None}
    count = len(_PDF_PAGE_MARKER.findall(raw))
    return {
        "pdf_sha256": _sha256_bytes(raw),
        "pdf_size_bytes": len(raw),
        "page_count": count or None,
    }


def _field_refresh_status(docx_raw: bytes) -> str:
    """Best-effort inspection of ``word/settings.xml``'s ``<w:updateFields>``
    flag -- whether Word will auto-refresh TOC/page-number/cross-reference
    fields the next time this document is opened (e.g. by a render backend).
    A document with fields that will NOT auto-refresh can render
    "successfully" while showing STALE field text, which is exactly the kind
    of gap a render receipt should surface rather than stay silent about.

    Returns one of:
      - ``"will_auto_update"`` -- ``<w:updateFields>`` is present with no
        explicit ``w:val`` (OOXML treats a present-without-value boolean
        attribute as true), or an explicit true-ish ``w:val``. A render is
        expected to reflect current field content.
      - ``"not_configured"`` -- ``word/settings.xml`` parses fine but has no
        ``<w:updateFields>`` element (or an explicit false-ish one) -- Word
        will NOT auto-refresh fields on open; a render may show stale
        TOC/page-number/cross-reference text.
      - ``"unknown"`` -- ``word/settings.xml`` is missing or unparseable --
        cannot determine either way.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(docx_raw)) as archive:
            settings_xml = archive.read("word/settings.xml")
    except (KeyError, zipfile.BadZipFile):
        return "unknown"
    try:
        root = ET.fromstring(settings_xml)
    except ET.ParseError:
        return "unknown"
    element = root.find(f"{{{_W_NS}}}updateFields")
    if element is None:
        return "not_configured"
    value = element.get(f"{{{_W_NS}}}val")
    if value is None:
        return "will_auto_update"
    return "will_auto_update" if value.strip().lower() in ("1", "true", "on") else "not_configured"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RenderReceipt:
    """A durable, single render (or degraded-override) receipt.

    ``kind`` distinguishes a real render attempt (``"render"``, the default)
    from an audited human override (``"degraded_override"``, written only by
    :func:`check_release_render_gate`) -- both live in the same ledger so the
    full history (including every override and why) is in one place.
    """

    receipt_id: str
    docx_path: str
    source_docx_sha256: str | None
    status: str
    backend: str | None
    backend_version: str | None
    backend_order: list[str]
    process_identity: dict[str, Any] | None
    pdf_sha256: str | None
    pdf_size_bytes: int | None
    page_count: int | None
    duration_seconds: float
    attempts: int
    timed_out: bool
    error_class: str | None
    field_refresh_status: str
    visual_qa: dict[str, Any]
    reason: str | None
    created_at: str
    created_at_epoch: float
    kind: str = "render"
    preflight: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_receipt_write_lock = threading.Lock()


def _read_receipts(receipts_path: str) -> dict[str, Any]:
    try:
        with open(receipts_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"receipts": {}}
    if not isinstance(data, dict):
        return {"receipts": {}}
    data.setdefault("receipts", {})
    if not isinstance(data["receipts"], dict):
        data["receipts"] = {}
    return data


def _write_receipts(receipts_path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(receipts_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = receipts_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, receipts_path)  # atomic on both POSIX and Windows


def _persist_receipt(receipts_path: str, receipt: dict[str, Any]) -> None:
    with _receipt_write_lock:
        data = _read_receipts(receipts_path)
        data["receipts"][receipt["receipt_id"]] = receipt
        _write_receipts(receipts_path, data)


def list_render_receipts(
    receipts_path: str, docx_path: str | None = None,
) -> list[dict[str, Any]]:
    """All receipts (render attempts AND degraded overrides) on file at
    ``receipts_path``, optionally filtered to one ``docx_path``, newest
    first. ``[]`` if the ledger doesn't exist yet or is empty. Never raises.
    """
    data = _read_receipts(receipts_path)
    rows = list(data["receipts"].values())
    if docx_path is not None:
        rows = [r for r in rows if r.get("docx_path") == docx_path]
    return sorted(rows, key=lambda r: r.get("created_at_epoch") or 0.0, reverse=True)


def render_with_receipt(
    docx_path: str,
    *,
    backends: Sequence[RenderBackend] = KNOWN_BACKENDS,
    max_retries: int = 1,
    receipts_path: str | None = None,
    visual_qa: dict[str, Any] | None = None,
    check_result: dict[str, Any] | None = None,
    preflight: bool = False,
    style_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run :func:`check_render_capability` (or reuse an already-computed
    result) and build a DURABLE render receipt that survives the backend's
    own temp directory being cleaned up.

    Args:
      docx_path:      The document to attempt to render.
      backends:        Configurable, ordered backend priority list (as
                       :func:`check_render_capability`); recorded verbatim
                       on the receipt as ``backend_order``. Ignored (only
                       used for the recorded ``backend_order`` field, not
                       consulted for a real render) when ``check_result`` is
                       given.
      max_retries:      Forwarded to :func:`check_render_capability`. Ignored
                       when ``check_result`` is given.
      receipts_path:    When given, the built receipt is persisted (atomic
                       JSON ledger write) to this path -- pass the SAME path
                       on every call for one document/project so
                       :func:`list_render_receipts` /
                       :func:`check_release_render_gate` see full history.
                       Omit to build a receipt without persisting it.
      visual_qa:        Optional explicit visual-QA verdict, e.g.
                       ``{"status": "verified", "reviewer": "...", "notes":
                       "..."}``. When omitted, defaults to an explicit
                       ``"not_reviewed"`` (render succeeded) or
                       ``"not_applicable"`` (render did not succeed) status
                       -- a successful BACKEND CONVERSION is never, by
                       itself, recorded as a visual QA pass.
       check_result:     Optional pre-computed :func:`check_render_capability`
                       result (same three-state dict shape) to build the
                       receipt from INSTEAD of running a fresh render --
                       for a caller (e.g. a write-time gate) that already
                       ran the check once and needs a durable receipt for
                       that SAME result, without rendering the document a
                       second time. When given, ``backends``/``max_retries``
                       are not used to run anything; ``duration_seconds`` on
                       the resulting receipt is ``0.0`` (no render was timed
                        by this call).
       preflight:       When true, run :func:`docs_intel.preflight_document`
                        before the backend. A deterministic package/equation
                        failure returns a failed receipt without starting the
                        expensive render. Ignored when ``check_result`` is
                        supplied because that result already represents a
                        completed render check.
       style_policy:    Optional equation-style policy forwarded to preflight.

    Returns the receipt as a dict: ``receipt_id``, ``docx_path``,
    ``source_docx_sha256`` (the DOCX's own content hash, so a receipt can
    later be checked against a possibly-since-edited file), ``status``
    (the same three-state value as ``check_render_capability``),
    ``backend``, ``backend_version`` (best-effort), ``backend_order``,
    ``process_identity`` (``{"pid", "owned"}`` for the word-com backend,
    ``None`` for soffice/others -- see :func:`_soffice_render`'s own note on
    why soffice's pid is not retained), ``pdf_sha256``/``pdf_size_bytes``/
    ``page_count`` (``None`` unless ``status == "rendered"``),
    ``duration_seconds``, ``attempts``, ``timed_out``, ``error_class``,
    ``field_refresh_status``, ``visual_qa``, ``reason``, ``created_at``
    (ISO-8601) / ``created_at_epoch`` (float), ``kind`` (``"render"``).

    Per this module's c44d245d contract, a backend attempt that timed out is
    already reported as ``"failed"`` by ``check_render_capability`` --
    restated here defensively as an unmissable invariant: a timed-out COM
    (or any) probe can never be recorded on a receipt as ``"rendered"``.
    """
    started = time.monotonic()
    now = time.time()
    backend_order = [b.name for b in backends]

    docx_raw = _read_file_bytes(docx_path) if docx_path else None
    source_docx_sha256 = _sha256_bytes(docx_raw) if docx_raw is not None else None
    field_refresh_status = _field_refresh_status(docx_raw) if docx_raw is not None else "unknown"

    if check_result is not None:
        result = check_result
        duration = 0.0
        backend_order = result.get("backend_order") or backend_order
        preflight_result = None
    elif preflight:
        preflight_result = docs_intel.preflight_document(
            docx_path,
            style_policy=style_policy,
        )
        if preflight_result.get("ready_for_render") is not True:
            reason = preflight_result.get("error") or (
                "equation/style findings: "
                + repr((preflight_result.get("equation_audit") or {}).get("findings_by_type", {}))
            )
            result = {
                "status": FAILED,
                "reason": f"preflight blocked render: {reason}",
                "backend_order": backend_order,
                "detail": {
                    "error_class": CORRUPTION_ERROR,
                    "timed_out": False,
                    "attempts": 1,
                    "preflight": preflight_result,
                },
            }
        else:
            result = check_render_capability(
                docx_path,
                backends=backends,
                max_retries=max_retries,
            )
        duration = time.monotonic() - started
    else:
        result = check_render_capability(docx_path, backends=backends, max_retries=max_retries)
        duration = time.monotonic() - started
        preflight_result = None

    status = result["status"]
    detail = result.get("detail") or {}
    backend_name = result.get("backend")
    timed_out = bool(detail.get("timed_out", False))
    if timed_out and status == RENDERED:  # pragma: no cover -- defensive only
        status = FAILED

    backend_version = detail.get("backend_version")
    if backend_version is None and status == RENDERED and backend_name == _SOFFICE_BACKEND.name:
        # A separate, independently-mockable step -- see _soffice_version's
        # own docstring for why this is never called from inside
        # _soffice_render itself.
        backend_version = _soffice_version()

    process_identity = None
    if backend_name == _WORD_COM_BACKEND.name:
        process_identity = {"pid": detail.get("pid"), "owned": detail.get("pid") is not None}

    if status == RENDERED:
        visual_qa_field = dict(visual_qa) if visual_qa else {
            "status": "not_reviewed",
            "note": (
                "backend conversion succeeded -- this is STRUCTURAL render "
                "success only, not a human/automated visual QA pass. Pass "
                "visual_qa= explicitly to record one; until then this "
                "receipt must never be read as visually verified."
            ),
        }
    else:
        visual_qa_field = dict(visual_qa) if visual_qa else {
            "status": "not_applicable",
            "note": f"no PDF was produced for this attempt (status={status!r})",
        }

    receipt = RenderReceipt(
        receipt_id=str(uuid.uuid4()),
        docx_path=docx_path,
        source_docx_sha256=source_docx_sha256,
        status=status,
        backend=backend_name,
        backend_version=backend_version,
        backend_order=backend_order,
        process_identity=process_identity,
        pdf_sha256=detail.get("pdf_sha256"),
        pdf_size_bytes=detail.get("pdf_size_bytes"),
        page_count=detail.get("page_count"),
        duration_seconds=duration,
        attempts=int(detail.get("attempts") or 1),
        timed_out=timed_out,
        error_class=detail.get("error_class"),
        field_refresh_status=field_refresh_status,
        visual_qa=visual_qa_field,
        reason=result.get("reason"),
        created_at=_utcnow_iso(),
        created_at_epoch=now,
        preflight=preflight_result,
    ).to_dict()

    if receipts_path:
        _persist_receipt(receipts_path, receipt)

    return receipt


def check_release_render_gate(
    docx_path: str,
    receipts_path: str,
    *,
    max_age_seconds: float = 24 * 3600.0,
    now: float | None = None,
    allow_degraded_override: bool = False,
    override_reason: str | None = None,
    override_by: str | None = None,
) -> dict[str, Any]:
    """Fail-closed release gate: a document may only be released with a
    FRESH, matching, real ``"rendered"`` receipt on file -- unless a human
    explicitly, auditedly overrides it as degraded.

    "Fresh" means the newest matching receipt in ``receipts_path`` (via
    :func:`list_render_receipts`) has ALL of:
      - ``status == "rendered"`` (never ``"unavailable-with-reason"`` or
        ``"failed"`` -- and since a timed-out probe already reports as
        ``"failed"``, it can never satisfy this gate as-is);
      - ``source_docx_sha256`` matching ``docx_path``'s CURRENT on-disk
        content (a receipt for a since-edited document is not evidence
        about the CURRENT content);
      - an age (``now - created_at_epoch``) no greater than
        ``max_age_seconds``.

    With no such receipt, the gate refuses (``release_ready=False``) unless
    ``allow_degraded_override=True`` is passed WITH a non-empty
    ``override_reason`` -- which records a second, durable, audited
    ``kind="degraded_override"`` receipt in the SAME ledger (never a silent
    bypass) and returns ``release_ready=True`` with ``degraded=True``.

    Returns ``{"release_ready": bool, "degraded": bool,
    "visually_verified": bool, "reason": str | None, "matched_receipt":
    dict | None, "override": dict | None}``. ``visually_verified`` is a
    DELIBERATELY SEPARATE, stricter signal from ``release_ready`` -- it is
    ``True`` only when the matched receipt's own ``visual_qa.status`` is
    ``"verified"`` -- so a caller can never mistake "fresh render evidence
    exists" (``release_ready``) for "a human/automated visual QA pass
    actually happened" (``visually_verified``); a plain backend-conversion
    success alone never sets the latter. Never raises.
    """
    if now is None:
        now = time.time()
    empty_override = {
        "release_ready": False, "degraded": False, "visually_verified": False,
        "matched_receipt": None, "override": None,
    }
    if not docx_path or not str(docx_path).strip():
        return {**empty_override, "reason": "docx_path is required"}
    if not receipts_path or not str(receipts_path).strip():
        return {**empty_override, "reason": "receipts_path is required"}

    docx_raw = _read_file_bytes(docx_path)
    current_sha256 = _sha256_bytes(docx_raw) if docx_raw is not None else None

    candidates = [
        r for r in list_render_receipts(receipts_path, docx_path=docx_path)
        if r.get("kind", "render") == "render"
    ]
    fresh: dict[str, Any] | None = None
    for receipt in candidates:  # newest first
        if receipt.get("status") != RENDERED:
            continue
        if current_sha256 is None or receipt.get("source_docx_sha256") != current_sha256:
            continue
        created_epoch = receipt.get("created_at_epoch")
        if created_epoch is None or (now - created_epoch) > max_age_seconds:
            continue
        fresh = receipt
        break

    if fresh is not None:
        visually_verified = (fresh.get("visual_qa") or {}).get("status") == "verified"
        return {
            "release_ready": True, "degraded": False,
            "visually_verified": visually_verified, "reason": None,
            "matched_receipt": fresh, "override": None,
        }

    if not candidates:
        reason = f"no render receipt on file for {docx_path!r} in {receipts_path!r}"
    else:
        reason = (
            "no FRESH matching rendered receipt on file -- every candidate "
            "was either not status=\"rendered\", recorded against a "
            "different docx content hash (the file has changed since), or "
            f"older than max_age_seconds={max_age_seconds!r}"
        )

    if not allow_degraded_override:
        return {**empty_override, "reason": reason}
    if not override_reason or not str(override_reason).strip():
        return {
            **empty_override,
            "reason": reason + "; allow_degraded_override=True but override_reason "
                                 "was empty -- refusing an unaudited override",
        }

    override_record = {
        "receipt_id": str(uuid.uuid4()),
        "kind": "degraded_override",
        "docx_path": docx_path,
        "source_docx_sha256": current_sha256,
        "gate_reason": reason,
        "override_reason": str(override_reason),
        "override_by": override_by,
        "created_at": _utcnow_iso(),
        "created_at_epoch": now,
    }
    _persist_receipt(receipts_path, override_record)
    return {
        "release_ready": True, "degraded": True, "visually_verified": False,
        "reason": reason, "matched_receipt": None, "override": override_record,
    }


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


# ---------------------------------------------------------------------------
# 3b3020ac -- execution-manifest-gated promotion readiness.
#
# meridian.executor_contract.aggregate_worker_completions() (a hash-pinned,
# fail-closed aggregation over a scientific fan-out run's per-worker
# completion records) is consumed here as a PLAIN DICT, duck-typed -- this
# package is a separate, optionally-installed extension and does not import
# meridian.executor_contract; a caller (Meridian core, or the thesis project
# directly) builds the aggregation and passes it straight in.
#
# A DOCX draft whose CONTENT is derived from a scientific run (a results
# table, a generated figure caption, a summary paragraph) must never be
# promoted to canonical on the strength of narrative notes or directory
# presence alone -- the run's own fail-closed aggregation must itself be
# "ok". This function is a THIN layer on top of :func:`verify_promotion_readiness`
# (called UNCHANGED, never duplicated): it adds exactly one more precondition
# -- the manifest gate -- to the existing fingerprint/structural/render
# checks, using the SAME ``{"ready": bool, "reason": str|None, ...}``
# fail-closed verdict shape this module already uses everywhere.
# ---------------------------------------------------------------------------

def verify_execution_manifest_promotion_readiness(
    canonical_path: str,
    draft_path: str,
    expected_source_sha256: str,
    aggregation: "dict[str, Any] | None",
    *,
    allow_partial_manifest: bool = False,
    require_render: bool = False,
    backends: Sequence[RenderBackend] = KNOWN_BACKENDS,
) -> dict[str, Any]:
    """:func:`verify_promotion_readiness`, PLUS a scientific execution-
    manifest completeness gate.

    ``aggregation`` is the dict returned by
    ``meridian.executor_contract.aggregate_worker_completions`` (or an
    equivalent caller-built dict carrying at least ``{"ok": bool, "status":
    str, "is_full_production": bool}``).

    The manifest gate refuses promotion (``ready=False``) when EITHER:

    * ``aggregation`` is missing, not a dict, or its own ``ok`` is falsy --
      narrative notes or directory presence are never sufficient evidence
      to promote a document derived from a scientific run.
    * ``aggregation`` is ``ok`` but NOT ``is_full_production`` (a valid
      failure-stage subset, per
      ``executor_contract.aggregate_worker_completions``'s own
      full-production-vs-subset distinction) and the caller did not pass
      ``allow_partial_manifest=True`` -- promoting a document from a
      PARTIAL run must be an explicit, opt-in choice, never the default.

    All of :func:`verify_promotion_readiness`'s existing checks (source-
    fingerprint equality, structural comparison, render verification) still
    run UNCHANGED and independently gate readiness too -- this function
    never weakens or bypasses any of them; it only ADDS the manifest check
    on top, mirroring how ``check_promotion_preconditions`` (24f5146d) added
    a base-hash gate without touching ``transactional_merge``'s own apply-
    time checks.

    Returns :func:`verify_promotion_readiness`'s own dict shape, plus one
    additive ``manifest_check`` key
    (``{"ok": bool, "status": str|None, "is_full_production": bool|None,
    "accepted": bool}``). ``ready``/``reason`` reflect BOTH the base checks
    AND the manifest gate (a semicolon-joined summary of every failing
    check, matching this module's existing convention). Never raises.
    """
    manifest_reasons: list[str] = []
    aggregation_ok = isinstance(aggregation, dict) and bool(aggregation.get("ok"))
    if not aggregation_ok:
        status = aggregation.get("status") if isinstance(aggregation, dict) else None
        manifest_reasons.append(
            f"execution-manifest aggregation is not ok (status={status!r}) -- "
            "narrative notes or directory presence are never sufficient "
            "evidence to promote a document derived from a scientific run"
        )
    elif not aggregation.get("is_full_production") and not allow_partial_manifest:
        manifest_reasons.append(
            f"execution-manifest aggregation status="
            f"{aggregation.get('status')!r} is not full production data (a "
            "valid failure-stage subset) -- pass allow_partial_manifest=True "
            "to explicitly accept promoting from a partial run"
        )

    manifest_check: dict[str, Any] = {
        "ok": aggregation_ok,
        "status": aggregation.get("status") if isinstance(aggregation, dict) else None,
        "is_full_production": (
            aggregation.get("is_full_production") if isinstance(aggregation, dict) else None
        ),
        "accepted": not manifest_reasons,
    }

    base = verify_promotion_readiness(
        canonical_path, draft_path, expected_source_sha256,
        require_render=require_render, backends=backends,
    )

    ready = bool(base["ready"]) and not manifest_reasons
    reasons: list[str] = []
    if not base["ready"] and base.get("reason"):
        reasons.append(base["reason"])
    reasons.extend(manifest_reasons)

    return {
        **base,
        "ready": ready,
        "reason": None if ready else "; ".join(reasons),
        "manifest_check": manifest_check,
    }
