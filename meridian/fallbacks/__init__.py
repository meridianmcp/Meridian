"""Render-capability detection for DOCX write verification (8d2ef784, DOCS-R2-A).

Ported from ``extensions/meridian-docs/meridian_docs/render_gate.py`` into
core Meridian-build so ``meridian/doc_store.py``'s writers (starting with
``update_paragraph``) can gate a promoted write on the same tri-state render
contract the extension already ships and tests. This is a deliberate,
independent module, not a cross-import: ``extensions/meridian-docs`` is a
separate, optionally-installed package and core ``meridian`` never imports
from it (the same no-cross-import convention ``doc_store.py``'s own module
docstring already documents for its write-side duplication with
``docs_intel.py``).

Why this exists: a structural write to a .docx (``_write_docx_transaction``'s
manifest gate, plus a caller-specific post-write check like
``_verify_paragraph_write``) proves the ZIP/XML is well-formed and the
intended text landed -- it says nothing about whether the document actually
*renders* the way a human reviewer would see it in Word. Before this module,
a caller had no way to distinguish three very different situations:

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
can never mistake "we couldn't check" for "we verified this renders", the
exact same three-state vocabulary ``extensions/meridian-docs/meridian_docs/
render_gate.py`` uses (sprint item 93cd9798 there).

Provenance / scope note (8d2ef784, re-derived against current dev -- see
``tests/test_fallback_contracts.py`` module docstring for the full account):
an earlier attempt at this port (orphaned commit ``e5045b3f``, Aug 9 2026,
never merged) fully duplicated BOTH backends -- LibreOffice/soffice AND a
second independent Word-COM automation implementation (a plain threaded
``pywin32`` driver). Two things have changed since then that make a verbatim
copy the wrong move now:

  1. ``render_gate.py``'s OWN Word-COM implementation has since moved from a
     simple thread to an isolated subprocess worker (PID-tracked, terminated
     on timeout) -- substantially more complex, Windows-COM-specific code
     that would be a fresh, untested THIRD copy if duplicated again here.
  2. ``tools/meridian_fallbacks/docx_completion_gate.py`` (landed 2026-08-05,
     postdating ``e5045b3f``) already solved exactly this problem for its own
     local completion-gate contract: rather than re-implementing Word-COM
     automation, its ``_default_render_checker`` does a guarded, dotted
     ``importlib`` delegation to
     ``meridian_docs.render_gate.check_word_com_render_receipt`` --
     degrading to :data:`UNAVAILABLE_WITH_REASON` (never a crash, never a
     silent success) whenever the extension isn't installed.

This module follows (2), not (1), for the Word-COM backend specifically:
:data:`_WORD_COM_BACKEND` delegates to
``meridian_docs.render_gate.check_word_com_render_receipt`` via the same
guarded-``importlib`` pattern, rather than re-implementing a third copy of
isolated-process Word automation. The LibreOffice/soffice backend IS still a
genuine, independent, stdlib+subprocess re-implementation (no delegation) --
that half is simple, has no Windows-COM complexity, and matches this
package's "deliberate duplicate, not a cross-import" design intent exactly.
A caller who explicitly wants the ORIGINAL extension's soffice implementation
(e.g. for its PDF-receipt-field capture) still has ``extensions/meridian-docs``
directly available; this module's soffice backend is deliberately narrower --
pass/fail render-capability detection only, no PDF hash/page-count receipt
fields -- since it exists to GATE a write, not to build a durable visual-QA
audit receipt (that remains ``render_gate.render_with_receipt``'s job).

See ``doc_store.py``'s ``update_paragraph`` (``check_render=True``,
``allow_degraded_render=``, ``degraded_render_reason=``) for the sole current
caller of :func:`check_render_capability` in core Meridian-build.
"""
from __future__ import annotations

import importlib
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
]

# ---------------------------------------------------------------------------
# The three-state contract (matches extensions/meridian-docs/meridian_docs/
# render_gate.py's contract exactly -- see sprint item 93cd9798 there).
# ---------------------------------------------------------------------------

RENDERED = "rendered"
UNAVAILABLE_WITH_REASON = "unavailable-with-reason"
FAILED = "failed"

RENDER_STATUSES: tuple[str, str, str] = (RENDERED, UNAVAILABLE_WITH_REASON, FAILED)

RENDER_TEMPDIR_PREFIX = "meridian_fallbacks_render_gate_"


# ---------------------------------------------------------------------------
# Bounded, diagnostic failure classification for the ``"failed"`` status.
# The three-state contract above stays exactly as-is -- this classifies *why*
# a ``"failed"`` result happened, carried as ``error_class`` on both the
# raised :class:`RenderCapabilityError` and the ``detail`` dict of the
# returned result. Matches render_gate.py's vocabulary string-for-string.
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

    * ``error_class`` -- one of :data:`FAILURE_CLASSES`. Defaults to
      ``UNKNOWN_ERROR`` so a message-only ``raise RenderCapabilityError("...")``
      still works.
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

    ``unavailable_reason`` must be cheap (a ``shutil.which`` / import-probe
    style lookup, never a slow subprocess render or COM automation call) --
    this module does capability DETECTION, not rendering. It returns ``None``
    when the backend is available, or a concrete, itemizable reason string
    when it is not.

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
# Backend 1: LibreOffice / OpenOffice headless conversion.
#
# A genuine, independent re-implementation (stdlib + subprocess only, no
# import of extensions/meridian-docs) -- deliberately narrower than
# render_gate.py's own soffice backend: this exists to gate a write, not to
# build a durable visual-QA receipt, so it skips PDF hash/page-count capture.
# ---------------------------------------------------------------------------

# Module-level so tests can monkeypatch a short bound instead of waiting out
# a real 60s timeout to exercise the timeout-classification path.
_SOFFICE_TIMEOUT_SECONDS = 60.0

# Substrings (lowercased) in soffice's stderr that indicate the SOURCE
# document itself is the problem (a genuinely corrupt/unreadable .docx), as
# opposed to a transient environment hiccup (profile lock contention, a busy
# display server, a momentarily-unavailable temp dir, etc.). Best-effort
# heuristic -- soffice has no machine-readable error-classification exit code.
_SOFFICE_CORRUPTION_MARKERS = (
    "source file could not be loaded",
    "not a valid",
    "corrupt",
    "damaged",
    "unreadable content",
    "cannot be read",
)


def _soffice_executable() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _soffice_unavailable_reason() -> str | None:
    if _soffice_executable() is not None:
        return None
    return "LibreOffice ('soffice'/'libreoffice') not found on PATH"


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
            # subprocess.run() already kills the EXACT child process IT
            # spawned before re-raising -- never touches any other soffice
            # instance running on the machine. Timeouts are never retried: a
            # render that hung once is likely to hang again.
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
# Backend 2: Word COM automation -- delegated, not duplicated.
#
# See module docstring for why this backend delegates to
# meridian_docs.render_gate.check_word_com_render_receipt (guarded, dotted
# importlib -- extensions/meridian-docs is an optional, separately-installed
# package, never a hard import) instead of re-implementing pywin32/COM
# automation a third time in this codebase.
# ---------------------------------------------------------------------------

def _word_com_delegate_unavailable_reason() -> str | None:
    """Cheap availability probe for the Word-COM backend.

    Mirrors ``render_gate._word_com_unavailable_reason``'s own cheap checks
    (platform + ``pywin32`` import probe -- never launches Word or performs
    COM automation), plus one additional cheap check this module needs that
    the extension's own function doesn't: whether the extension itself is
    even importable, since delegation only works when it is.
    """
    if sys.platform != "win32":
        return f"Word COM automation is only available on win32 (current platform: {sys.platform})"
    try:
        import win32com.client  # noqa: F401  (import-only availability probe)
    except ImportError:
        return "pywin32 (win32com) is not installed -- Word COM automation unavailable"
    try:
        module = importlib.import_module("meridian_docs.render_gate")
    except Exception as exc:  # noqa: BLE001 -- optional sibling; any import failure degrades
        return (
            "meridian_docs.render_gate is not importable in this environment "
            f"(extensions/meridian-docs is an optional, separately-installed "
            f"package): {type(exc).__name__}: {exc}"
        )
    if not callable(getattr(module, "check_word_com_render_receipt", None)):
        return (
            "meridian_docs.render_gate.check_word_com_render_receipt is not "
            "available in the installed meridian_docs version"
        )
    return None


def _word_com_delegate_render(docx_path: str) -> dict[str, Any]:
    """Delegate the actual render attempt to
    ``meridian_docs.render_gate.check_word_com_render_receipt`` and translate
    its own tri-state result into this module's ``RenderBackend`` contract
    (return a detail dict on success, raise :class:`RenderCapabilityError`
    otherwise).

    ``_word_com_delegate_unavailable_reason`` above already filters out the
    common unavailable cases (non-Windows, no pywin32, extension not
    installed) before this is ever called. In the rare case the delegate
    itself STILL reports ``"unavailable-with-reason"`` at call time (e.g. the
    extension is importable but Word itself isn't installed on this
    machine -- a check this module's own cheap probe deliberately does not
    duplicate, since detecting a real Word installation cheaply isn't
    possible), that is folded into a ``"failed"`` result here (via
    :class:`RenderCapabilityError`) rather than silently reported as
    available -- never treated as ``"rendered"``.
    """
    module = importlib.import_module("meridian_docs.render_gate")
    checker = module.check_word_com_render_receipt
    try:
        raw_result = checker(docx_path)
    except Exception as exc:  # noqa: BLE001 -- a checker must never crash this gate
        raise RenderCapabilityError(
            f"meridian_docs.render_gate.check_word_com_render_receipt raised: "
            f"{type(exc).__name__}: {exc}",
            error_class=UNKNOWN_ERROR,
        ) from exc
    if not isinstance(raw_result, dict) or "status" not in raw_result:
        raise RenderCapabilityError(
            "meridian_docs.render_gate.check_word_com_render_receipt returned "
            "an unexpected (non-dict, or missing 'status') result",
            error_class=UNKNOWN_ERROR,
        )
    status = raw_result.get("status")
    if status == RENDERED:
        detail = dict(raw_result.get("detail") or {})
        detail["delegate_backend"] = raw_result.get("backend")
        return detail
    # "failed" or "unavailable-with-reason" (or any unrecognized status) --
    # both fold into RenderCapabilityError here; check_render_capability's
    # generic wrapper turns that into this module's own "failed" status.
    detail = raw_result.get("detail") or {}
    error_class = detail.get("error_class", UNKNOWN_ERROR)
    if error_class not in FAILURE_CLASSES:
        error_class = UNKNOWN_ERROR
    reason = raw_result.get("reason") or f"delegate reported status={status!r}"
    raise RenderCapabilityError(
        reason,
        error_class=error_class,
        exit_code=detail.get("exit_code"),
        stderr=detail.get("stderr"),
        timed_out=bool(detail.get("timed_out")),
        retryable=False,  # never auto-retry Word COM automation from this layer
    )


_WORD_COM_BACKEND = RenderBackend(
    name="word-com",
    unavailable_reason=_word_com_delegate_unavailable_reason,
    render=_word_com_delegate_render,
)

KNOWN_BACKENDS: tuple[RenderBackend, ...] = (_SOFFICE_BACKEND, _WORD_COM_BACKEND)


def check_word_com_render_receipt(docx_path: str) -> dict[str, Any]:
    """Like :func:`check_render_capability`, but restricted to the Word COM
    backend only (never LibreOffice/soffice) -- mirrors
    ``render_gate.check_word_com_render_receipt``'s own narrowing helper.

    Returns the SAME three-state contract (``"rendered"`` /
    ``"unavailable-with-reason"`` / ``"failed"``), scoped to just the
    ``"word-com"`` backend: on any non-Windows platform, a Windows machine
    without ``pywin32``/Word, or when ``extensions/meridian-docs`` isn't
    installed, this returns ``"unavailable-with-reason"`` -- never
    ``"rendered"``.
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


def _failure_detail(
    exc: RenderCapabilityError | None, *, attempts: int, exception_type: str | None = None
) -> dict[str, Any]:
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
        NEVER folded into ``"unavailable-with-reason"``.

    A missing/invalid ``docx_path`` is also reported as ``"failed"`` (a
    concrete, checkable error about this specific call), not
    ``"unavailable-with-reason"`` (reserved for environment-level capability
    gaps that are true regardless of which document was passed).

    A ``"failed"`` result's ``detail`` always carries ``error_class`` (one of
    :data:`FAILURE_CLASSES`), ``timed_out``, and ``attempts``, plus
    ``exit_code``/``stderr`` when the backend captured them.
    ``max_retries`` (default 1) bounds automatic retry of a render attempt,
    and ONLY when the immediately-preceding failure classified itself as
    ``retryable=True`` (a transient, idempotent transport hiccup -- rendering
    never mutates ``docx_path``, so retrying is always safe from a data
    standpoint). Timeouts and document-corruption failures are never
    retryable and so are never retried, no matter how high ``max_retries`` is
    set.

    Every returned dict (all three statuses) also carries ``backend_order``:
    the full, ordered list of backend NAMES this call was configured with. A
    ``"rendered"`` result's ``detail`` also always carries ``attempts`` (how
    many render attempts, including retries, it took to succeed).
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
        except Exception as exc:  # noqa: BLE001 -- backend bug / unexpected error
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
