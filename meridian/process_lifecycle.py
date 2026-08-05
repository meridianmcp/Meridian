"""3c4ed79d — portable owned-process lifecycle backends.

Meridian spawns local child processes it needs to track and tear down
cleanly: tunnel proxy slots, Serena daemons, custom plugin servers. Today
teardown (``tunnel_client._terminate_proc_tree``) targets a single PID with
a Windows ``taskkill /F /T`` / POSIX terminate-then-kill escalation. That is
fragile against two real failure modes:

* A child that spawns grandchildren before Meridian ever gets a chance to
  record/kill it -- reparented descendants aren't reachable by killing the
  root PID alone.
* PID reuse: a PID read back later (e.g. from a persisted registry) may by
  then belong to a completely unrelated process the OS recycled the number
  for. Killing on PID alone, without checking identity, risks killing the
  wrong thing.

This module gives every "owned" spawn an actual OS-level container instead
of relying on process-NAME matching (explicitly out of scope per the sprint
notes -- "No name-based cleanup"):

* **Windows** -- a Job Object (``CreateJobObject`` +
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, with no breakaway limit set, so a
  child can never detach itself from the job even if it launches a
  grandchild with ``CREATE_BREAKAWAY_FROM_JOB`` -- that flag only works when
  the job itself opts in via ``JOB_OBJECT_LIMIT_(SILENT_)BREAKAWAY_OK``,
  which we never set). ``TerminateJobObject`` kills every process still
  assigned to the job in one syscall -- the whole tree, not just the direct
  child -- and we ALSO run the existing guarded ``taskkill /F /T`` sweep
  underneath it as a fallback for anything that raced its way out before
  assignment completed (see :class:`WindowsJobObjectBackend`).
* **POSIX** -- a new session/process group (``start_new_session=True``,
  equivalent to calling ``setsid()`` before exec), so the whole tree can be
  signalled at once via ``os.killpg`` instead of only the root PID, with a
  graceful ``SIGTERM`` escalating to ``SIGKILL`` if the group is still alive
  after a grace period.

Both backends implement the same small two-method interface (``spawn`` /
``adopt`` / ``close``) and hand back an :class:`OwnedProcessHandle` carrying
enough identity -- a Meridian-generated ``run_id``, the root ``pid``, the
platform's tree/group identity, ``executable``/``cwd``/``cmdline``, and (when
``psutil`` is available) the process's own ``create_time`` -- that a LATER,
separate process can serialize/deserialize the handle
(:meth:`OwnedProcessHandle.to_dict` / :meth:`from_dict`) and verify it still
refers to the SAME process before acting on it (:func:`verify_handle_live`)
-- the PID-reuse guard the sprint's acceptance criteria calls out
explicitly.

Deliberately NOT included here (left to the dependent sprint item,
7dce2cf1): a persisted, cross-run registry of every owned handle and the
orphan-reaper rewrite that consumes it. This module is the portable
lifecycle *primitive* only. ``tunnel_client.py`` wires ONE new, additive
spawn/teardown pair on top of it
(``_spawn_owned_with_cache_retry`` / ``_close_owned_process``) without
touching any EXISTING call site's behaviour -- ``_spawn_kwargs()`` and
``_terminate_proc_tree()``, used by every current caller, are unchanged, so
"ownership semantics for unrelated processes" cannot regress.

Windows Job Object mechanics use raw ``ctypes`` bindings to ``kernel32``
(no ``pywin32`` dependency) behind an **injectable loader**
(:func:`_load_win32_job_api`, overridable via ``WindowsJobObjectBackend``'s
``api_loader`` constructor arg) so tests that monkeypatch
``sys.platform = "win32"`` to exercise the Windows code path on Linux CI
never touch a real ``ctypes.WinDLL`` (which does not exist off Windows) --
the same "must not touch Windows-only stdlib attrs on a monkeypatched
platform" trap already documented in ``tests/test_tunnel_client.py``.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Owned-process identity
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Generate a fresh Meridian-side owned-run identifier -- independent
    of (and stable across) any OS pid reuse."""
    return uuid.uuid4().hex


def _safe_create_time(pid: int) -> "float | None":
    """Best-effort ``psutil`` create_time lookup for *pid*. ``None`` when
    psutil is unavailable or the process has already exited -- never
    raises. Same optional-psutil degrade pattern used throughout
    ``tunnel_client.py``."""
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001
        return None


@dataclass
class OwnedProcessHandle:
    """Identity + lifecycle handle for a process (tree) a
    :class:`ProcessLifecycleBackend` spawned/adopted and now owns.

    Fields mirror the sprint's acceptance criteria: a Meridian-generated
    ``run_id`` (independent of the OS pid, so it stays a stable identity
    across a PID-reuse scenario), the root ``pid``, the platform's own
    tree/group identity (POSIX process-group id in ``group_id``, or the
    opaque Windows job handle in ``job_id``), the ``executable``/``cwd``/
    ``cmdline`` that was launched, and -- when ``psutil`` is available --
    the process's own ``create_time`` for PID-reuse protection.

    ``popen`` is the live in-process handle, kept only for the process that
    actually did the spawning; it is excluded from equality/repr and from
    :meth:`to_dict` so a handle can be persisted and read back by a
    DIFFERENT process (the dependent registry item, 7dce2cf1) without
    trying to serialize a live OS object that means nothing there.

    ``closed`` makes :meth:`ProcessLifecycleBackend.close` idempotent: once
    True, a repeat ``close()`` call is a confirmed no-op rather than
    re-signalling (potentially a PID the OS has since reused).
    """

    run_id: str
    pid: int
    executable: str
    cwd: "str | None"
    cmdline: "list[str]"
    create_time: "float | None" = None
    group_id: "int | None" = None  # POSIX process-group id (== pid when leader)
    job_id: "int | None" = None  # opaque Windows job handle, if job assignment succeeded
    popen: "subprocess.Popen | None" = field(default=None, repr=False, compare=False)
    closed: bool = False

    def to_dict(self) -> "dict[str, Any]":
        """Serializable snapshot -- everything except the live ``popen``
        handle, which only means something inside the process that spawned
        it."""
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "executable": self.executable,
            "cwd": self.cwd,
            "cmdline": list(self.cmdline),
            "create_time": self.create_time,
            "group_id": self.group_id,
            "job_id": self.job_id,
            "closed": self.closed,
        }

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "OwnedProcessHandle":
        """Reconstruct a handle from :meth:`to_dict` output (e.g. read back
        from a persisted registry by a different process than the one that
        spawned it). ``popen`` is always ``None`` on the reconstructed
        handle -- there is no live object to attach."""
        return cls(
            run_id=str(data["run_id"]),
            pid=int(data["pid"]),
            executable=str(data.get("executable") or ""),
            cwd=data.get("cwd"),
            cmdline=list(data.get("cmdline") or []),
            create_time=data.get("create_time"),
            group_id=data.get("group_id"),
            job_id=data.get("job_id"),
            popen=None,
            closed=bool(data.get("closed", False)),
        )


def verify_handle_live(handle: OwnedProcessHandle) -> bool:
    """PID-reuse guard: True iff *handle* still refers to the SAME process
    it was created for.

    Compares the process currently holding ``handle.pid`` against the
    recorded ``create_time`` (1s tolerance -- the same tolerance
    ``tunnel_client._kill_all_previously_spawned_pids`` already uses for
    this exact check). Degrades to ``True`` when ``psutil`` is unavailable
    OR the handle never recorded a ``create_time`` (nothing to verify
    against); returns ``False`` only on a confirmed mismatch or a
    confirmed-gone process. Never raises.
    """
    if handle.create_time is None:
        return True
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return True
    try:
        proc = psutil.Process(handle.pid)
        return abs(proc.create_time() - float(handle.create_time)) < 1.0
    except Exception:  # noqa: BLE001 — process gone / lookup failed
        return False


# ---------------------------------------------------------------------------
# POSIX backend — process group (setsid) + signal escalation
# ---------------------------------------------------------------------------

# os.killpg and signal.SIGKILL do not exist AT ALL on Windows (unlike e.g.
# signal.SIGTERM, which Windows does define) -- accessing them as bare
# attributes raises AttributeError regardless of which platform actually
# ends up USING PosixProcessGroupBackend. Resolve them once, via getattr
# with a literal POSIX-correct fallback, exactly mirroring the existing
# getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) pattern in
# tunnel_client._spawn_kwargs() -- this keeps the module (and this class)
# importable/constructible/testable on a Windows dev box or CI runner even
# though get_default_backend() only ever selects this backend on real
# POSIX. _killpg is None on a platform without os.killpg; every call site
# below guards for that and degrades to a best-effort failure, never a
# hard crash.
_killpg = getattr(os, "killpg", None)
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)


class PosixProcessGroupBackend:
    """POSIX owned-process backend: each spawn becomes its own session AND
    process-group leader (``start_new_session=True`` == ``setsid()`` before
    exec -- session id and process-group id both equal the new pid), so the
    whole tree can be torn down with one ``os.killpg`` instead of only the
    root pid. ``close()`` is graceful-then-forced (``SIGTERM``, escalate to
    ``SIGKILL`` if the group is still alive after ``grace_seconds``) and
    idempotent -- signalling an already-gone group (``ProcessLookupError``)
    is treated as success, never raised to the caller.
    """

    def spawn(
        self,
        cmd: "list[str]",
        *,
        env: "dict | None" = None,
        cwd: "str | None" = None,
        popen_kwargs: "dict | None" = None,
    ) -> OwnedProcessHandle:
        kwargs = dict(popen_kwargs or {})
        kwargs.setdefault("start_new_session", True)
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, **kwargs)
        return self.adopt(proc, cmd=cmd, cwd=cwd)

    def adopt(
        self, proc: "subprocess.Popen", *, cmd: "list[str]", cwd: "str | None" = None
    ) -> OwnedProcessHandle:
        """Build an :class:`OwnedProcessHandle` for an ALREADY-SPAWNED
        *proc* this backend did not itself ``Popen`` (e.g. a caller that
        needs its own retry/diagnostics machinery around the spawn, like
        ``tunnel_client._spawn_owned_with_cache_retry``). Assumes the
        caller already arranged for *proc* to be its own session/process-
        group leader (passed ``start_new_session=True`` to ``Popen``) --
        this method does NOT retroactively change a running process's
        group, which POSIX does not support from outside without the
        process's own cooperation.
        """
        pid = proc.pid
        return OwnedProcessHandle(
            run_id=new_run_id(),
            pid=pid,
            executable=cmd[0] if cmd else "",
            cwd=cwd,
            cmdline=list(cmd),
            create_time=_safe_create_time(pid),
            group_id=pid,
            popen=proc,
        )

    def close(self, handle: OwnedProcessHandle, *, grace_seconds: float = 5.0) -> bool:
        """Idempotent graceful-then-forced close of *handle*'s whole
        process group. Returns True once the group is confirmed gone (or
        was already gone / already closed)."""
        if handle.closed:
            return True
        if not verify_handle_live(handle):
            # Recorded create_time no longer matches -- pid was reused by
            # an unrelated process since this handle was made. Never
            # signal it.
            handle.closed = True
            return True
        pgid = handle.group_id if handle.group_id is not None else handle.pid
        ok = self._signal_and_wait(handle, pgid, _SIGTERM, grace_seconds)
        if not ok:
            ok = self._signal_and_wait(handle, pgid, _SIGKILL, grace_seconds)
        handle.closed = True
        return ok

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        if _killpg is None:
            return False  # no way to signal/check on this platform -- treat as gone
        try:
            _killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:  # noqa: BLE001 — e.g. PermissionError: assume still alive
            return True

    def _signal_and_wait(
        self, handle: OwnedProcessHandle, pgid: int, sig: int, grace_seconds: float
    ) -> bool:
        if _killpg is None:
            return True  # nothing we can do on this platform -- don't hang the caller
        try:
            _killpg(pgid, sig)
        except ProcessLookupError:
            return True  # already gone
        except Exception:  # noqa: BLE001 — best-effort, never raise to caller
            pass
        if handle.popen is not None:
            try:
                handle.popen.wait(timeout=grace_seconds)
                return True
            except Exception:  # noqa: BLE001 — subprocess.TimeoutExpired or similar
                pass
        else:
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                if not self._group_alive(pgid):
                    return True
                time.sleep(0.1)
        return not self._group_alive(pgid)


def enable_child_subreaper(libc_loader: "Callable[[], Any] | None" = None) -> bool:
    """Opt-in: mark the CURRENT process as a Linux child-subreaper
    (``prctl(PR_SET_CHILD_SUBREAPER, 1)``) so orphaned/reparented
    descendants of a killed intermediate process attach to THIS process
    instead of PID 1, where Meridian can no longer observe or reap them.

    This is process-global and affects every future descendant of the
    calling process -- it is NOT something :meth:`PosixProcessGroupBackend.spawn`
    enables automatically per-child. Callers opt in explicitly, only where
    reparented descendants genuinely need to stay observable (matches the
    sprint notes: "optional child-subreaper support only where reparented
    descendants must be observed"). No-op (returns False) on non-Linux or
    when libc/ctypes is unavailable; never raises.

    *libc_loader* is the test injection point (a zero-arg callable
    returning a ctypes-CDLL-like object exposing ``.prctl``); defaults to
    the real ``ctypes.CDLL(None)`` (the process's own libc) so tests never
    need to touch the real syscall.
    """
    if sys.platform not in ("linux", "linux2"):
        return False
    loader = libc_loader or (lambda: ctypes.CDLL(None, use_errno=True))  # type: ignore[call-overload]
    _PR_SET_CHILD_SUBREAPER = 36
    try:
        libc = loader()
        result = libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        return int(result) == 0
    except Exception:  # noqa: BLE001 — best-effort, never raise
        return False


# ---------------------------------------------------------------------------
# Windows backend — Job Object with KILL_ON_JOB_CLOSE, no breakaway
# ---------------------------------------------------------------------------

# JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE -- deliberately the ONLY limit flag we
# ever set on the job. We never set JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x800) or
# JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (0x1000) -- the sprint's "no-breakaway
# policy": a child cannot escape the job even if it launches a grandchild
# with CREATE_BREAKAWAY_FROM_JOB, because that flag has no effect unless the
# JOB ITSELF opted in via one of the two limits above, which it never does
# here.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800  # never set — documents the policy
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000  # never set — documents the policy
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Win32JobAPI:
    """Thin, injectable wrapper over the handful of ``kernel32`` calls Job
    Object management needs. A real instance (built by
    :func:`_load_win32_job_api`) wraps a properly-prototyped
    ``ctypes.WinDLL``; tests construct a fake object exposing the same
    method names so the Windows code path is fully exercisable on
    non-Windows CI (see module docstring)."""

    def __init__(self, kernel32: Any):
        self._k = kernel32

    def create_job(self) -> "int | None":
        h = self._k.CreateJobObjectW(None, None)
        return int(h) if h else None

    def set_kill_on_close(self, job_handle: int) -> bool:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return bool(ok)

    def open_process(self, pid: int) -> "int | None":
        h = self._k.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, int(pid))
        return int(h) if h else None

    def assign_process(self, job_handle: int, process_handle: int) -> bool:
        return bool(self._k.AssignProcessToJobObject(job_handle, process_handle))

    def terminate_job(self, job_handle: int, exit_code: int = 1) -> bool:
        return bool(self._k.TerminateJobObject(job_handle, exit_code))

    def close_handle(self, handle: int) -> bool:
        return bool(self._k.CloseHandle(handle))


def _load_win32_job_api() -> "Win32JobAPI | None":
    """Real loader: binds + prototypes ``ctypes.WinDLL('kernel32')``.
    Returns ``None`` (never raises) off Windows, or if the bind fails for
    any reason -- :class:`WindowsJobObjectBackend` degrades to
    taskkill-only teardown in that case, identical to the pre-existing
    baseline behaviour. Restype/argtypes are set explicitly to
    pointer-sized ``c_void_p`` for every handle so a 64-bit HANDLE is never
    silently truncated by ctypes' default ``c_int`` return-type assumption.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        return Win32JobAPI(kernel32)
    except Exception:  # noqa: BLE001
        return None


class WindowsJobObjectBackend:
    """Windows owned-process backend: assigns each spawn to a fresh Job
    Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` set and no breakaway
    limits, so ``close()`` tears down the WHOLE tree in one
    ``TerminateJobObject`` call, then ALWAYS ALSO runs the existing guarded
    ``taskkill /F /T`` sweep as a belt-and-suspenders fallback -- this
    covers a process assigned a moment too late to make the job (e.g. a
    grandchild spawned in the brief window between ``Popen`` returning and
    ``AssignProcessToJobObject`` completing, or the job/ctypes API being
    unavailable at all) and matches
    ``tunnel_client._terminate_proc_tree``'s existing, already-proven
    fallback exactly.

    Windows has no direct SIGTERM equivalent for an arbitrary,
    non-cooperating process tree, so "graceful then forced" here means:
    let the job's own teardown run first, then guarantee completion via
    taskkill. Degrades cleanly to taskkill-only teardown (``job_id`` stays
    ``None``) if the Job Object API is unavailable for any reason
    (non-Windows, ``ctypes`` bind failure, ``CreateJobObjectW``/
    ``AssignProcessToJobObject`` failure) -- ``spawn()``/``adopt()`` never
    fail just because job-object setup failed.
    """

    def __init__(self, api_loader: "Callable[[], Win32JobAPI | None] | None" = None):
        self._api_loader = api_loader or _load_win32_job_api

    def spawn(
        self,
        cmd: "list[str]",
        *,
        env: "dict | None" = None,
        cwd: "str | None" = None,
        popen_kwargs: "dict | None" = None,
    ) -> OwnedProcessHandle:
        kwargs = dict(popen_kwargs or {})
        # Mirrors tunnel_client._spawn_kwargs(): own process group so a
        # console Ctrl+C doesn't broadcast into the child tree.
        kwargs.setdefault(
            "creationflags", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, **kwargs)
        return self.adopt(proc, cmd=cmd, cwd=cwd)

    def adopt(
        self, proc: "subprocess.Popen", *, cmd: "list[str]", cwd: "str | None" = None
    ) -> OwnedProcessHandle:
        """Build an :class:`OwnedProcessHandle` for an ALREADY-SPAWNED
        *proc* and best-effort assign it to a fresh Job Object right now.
        There is an inherent race for a caller that didn't spawn via THIS
        backend's own :meth:`spawn`: *proc* may already have created
        grandchildren before this runs, and those grandchildren will not
        be captured by the job -- the guarded taskkill /T fallback in
        :meth:`close` is what covers that gap, exactly like
        ``tunnel_client._terminate_proc_tree`` already does
        unconditionally today."""
        pid = proc.pid
        handle = OwnedProcessHandle(
            run_id=new_run_id(),
            pid=pid,
            executable=cmd[0] if cmd else "",
            cwd=cwd,
            cmdline=list(cmd),
            create_time=_safe_create_time(pid),
            popen=proc,
        )
        self._assign_to_job(handle)
        return handle

    def _assign_to_job(self, handle: OwnedProcessHandle) -> None:
        """Best-effort: create a job, set KILL_ON_JOB_CLOSE, assign
        *handle*'s pid to it. Any failure at any step leaves
        ``handle.job_id`` as ``None`` (taskkill-only teardown) and never
        raises -- this runs right after a successful spawn/adopt; it must
        not turn a working spawn into a failed one."""
        api = self._api_loader()
        if api is None:
            return
        try:
            job = api.create_job()
            if job is None:
                return
            if not api.set_kill_on_close(job):
                api.close_handle(job)
                return
            proc_handle = api.open_process(handle.pid)
            if proc_handle is None:
                api.close_handle(job)
                return
            if not api.assign_process(job, proc_handle):
                api.close_handle(job)
                return
            handle.job_id = job
        except Exception:  # noqa: BLE001 — best-effort, never raise
            handle.job_id = None

    def close(self, handle: OwnedProcessHandle, *, grace_seconds: float = 5.0) -> bool:
        if handle.closed:
            return True
        if not verify_handle_live(handle):
            handle.closed = True
            return True
        api = self._api_loader()
        if api is not None and handle.job_id is not None:
            try:
                api.terminate_job(handle.job_id, 1)
                api.close_handle(handle.job_id)
            except Exception:  # noqa: BLE001
                pass
        # Guarded taskkill /T fallback -- always run regardless of whether
        # the job existed/succeeded, mirrors
        # tunnel_client._terminate_proc_tree exactly.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(handle.pid)],
                capture_output=True, check=False,
            )
        except Exception:  # noqa: BLE001
            pass
        if handle.popen is not None:
            try:
                handle.popen.wait(timeout=grace_seconds)
            except Exception:  # noqa: BLE001
                try:
                    handle.popen.kill()
                except Exception:  # noqa: BLE001
                    pass
        handle.closed = True
        return True


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def get_default_backend() -> "PosixProcessGroupBackend | WindowsJobObjectBackend":
    """Select the portable owned-process lifecycle backend for the current
    platform."""
    if sys.platform == "win32":
        return WindowsJobObjectBackend()
    return PosixProcessGroupBackend()
