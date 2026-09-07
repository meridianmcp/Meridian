"""899936dd -- LOCAL-RUNNER-FOUNDATION: a local-first Meridian runner.

Grounded in docs/meridian-local-runner-tunnel-investigation-2026-08-31.md
(not present in this checkout at the time of writing -- see this module's
own design notes below) and independent audits of the three modules this
item names as touch points: ``process_lifecycle.py`` (portable owned-process
identity + teardown), ``process_registry.py`` (cross-client lease broker),
and ``tunnel_lifecycle.py`` (tunnel connection readiness state machine).
None of those three modules is modified by this item -- everything here is
new, additive composition on top of their existing, already-tested public
(and, following the precedent set by ``tunnel_preflight.py`` importing
``tunnel_client``'s private classifiers, a couple of private) surfaces.

What this module IS: a small, local-machine supervisor for ONE child
process Meridian (or an external client acting on its behalf) wants
running persistently -- typically ``python -m meridian --mcp`` or a tunnel
wrapper invocation -- plus the diagnostic/control surface a human or a
script needs to manage it *without* requiring a live tunnel/cloud
connection at all. Every operation below is local-first: it works purely
against on-disk state + OS process introspection.

Design pillars (mapping directly to the sprint item's acceptance criteria):

1. **Separated status schema** -- :class:`RunnerStatus` has three
   independent sub-sections: :class:`ChildProcessStatus` (the owned OS
   process this runner spawned), :class:`LocalMcpStatus` (whether the
   child's local MCP endpoint is confirmed usable -- via an injected,
   optional ``health_probe`` callable, never guessed), and
   :class:`TunnelReadinessStatus` (delegates to the existing
   ``tunnel_lifecycle`` state machine for a configured tunnel slot label,
   or reports ``not_configured`` when no tunnel is wired to this runner).
   A caller can reason about "is the process alive", "is the local MCP
   endpoint usable", and "is the remote tunnel ready" as three genuinely
   independent questions -- a crashed tunnel never masks a healthy local
   child, and vice versa.

2. **One instance per (machine, scope), identity-aware, never port-based**
   -- :meth:`LocalRunner.start` refuses (:class:`RunnerAlreadyRunningError`)
   to spawn a second child for a scope whose previous :class:`RunnerRecord`
   is still verified alive (PID + ``create_time`` cross-check, reusing
   ``process_lifecycle.verify_handle_live``'s exact guard, extended here to
   a tri-state result -- see :meth:`LocalRunner._is_pid_alive` -- so an
   *unverifiable* prior record is treated as "possibly still running", not
   silently assumed dead) unless ``force=True`` is passed explicitly. A
   record whose process is CONFIRMED gone is automatically recovered (no
   ``force`` needed) -- this is the "stale PID" / "recovery" behaviour the
   sprint's test list calls out by name. On top of the runner's own
   authoritative on-disk record (recoverable by a completely different
   process/invocation -- it persists ``run_id``/``pid``/``create_time``/
   ``group_id``/``job_id``, the exact identity fields
   ``process_lifecycle.OwnedProcessHandle`` already defines), this module
   ALSO registers the child with ``process_registry.ProcessLeaseBroker``
   under one fixed client name (so Claude Code, Codex, Cursor, or a bare
   CLI invocation all share the same exclusivity domain for a given scope)
   for cross-tool visibility. Nothing in this module ever discovers or
   kills a process by scanning a listening port -- every teardown call goes
   through an identity-verified :class:`process_lifecycle.OwnedProcessHandle`
   reconstructed from persisted, verified identity fields.

3. **Bounded local operations, no tunnel required** -- :meth:`status` and
   :meth:`doctor` are O(1)-ish: one state-file read, one liveness check, no
   sleeping or looping. The only operation that can legitimately wait is
   :meth:`start`/:meth:`restart` (bounded by ``cold_start_timeout``, and
   only when a ``health_probe`` is actually configured -- with no probe,
   the bound collapses to the small, fixed ``crash_settle_seconds`` window
   used purely to catch a fast dependency-missing crash). :meth:`tail_log`
   never reads more than ``max_bytes`` off the end of the log file
   regardless of how large the file has grown, and every log file is
   scoped to one run (pruned to the ``max_log_files`` most recent per
   scope) rather than growing without bound across restarts.

4. **Script escape hatch, explicitly scoped** -- :class:`ScriptAllowlist` /
   :func:`run_allowlisted_script` never execute an arbitrary,
   caller-supplied command string. Every invocation resolves a *name*
   against a pre-declared :class:`ScriptSpec`; an undeclared name (or
   extra args a spec didn't opt into) raises :class:`ScriptNotAllowedError`
   before anything is spawned. Every run produces a :class:`ScriptReceipt`
   with two projections: :meth:`ScriptReceipt.to_local_dict` (full detail,
   for local use only) and :meth:`ScriptReceipt.to_shared_projection`
   (redacts anything secret-shaped or an absolute local path, reusing
   ``capability_manifest``'s own secret pattern -- the same "no secrets or
   machine-local paths in shared Meridian state" rule that module already
   enforces for capability manifests) -- the projection a caller should use
   if a receipt is ever attached to shared project state (a note, a
   decision, a handoff). This module itself never writes to the Meridian
   DB; the allowlist and its receipts are local-machine JSON, exactly like
   ``process_registry``'s own lease file.

5. **Compatibility** -- this module is purely additive. It does not modify
   ``meridian/__main__.py``, ``meridian/server.py``, or
   ``meridian/tunnel_client.py``, so ``meridian --mcp``, ``meridian
   --tunnel``, and every existing stdio MCP configuration are byte-for-byte
   unchanged. It exposes its own standalone CLI (``python -m
   meridian.local_runner <command>``), mirroring the exact precedent
   ``process_registry.py`` already set for a client-neutral, scriptable
   JSON control surface external tooling (or a future ``--runner`` wiring
   into ``__main__.py``, deliberately left as follow-up scope) can drive
   without importing Python.

See ``tests/test_local_runner.py`` for the acceptance-criteria test list
this module is built against: duplicate launch, stale PID, child crash,
bounded output, cold-start timeout, and recovery.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import process_lifecycle
from . import process_registry
from . import tunnel_lifecycle
from . import tunnel_preflight
from .capability_manifest import _SECRET_LIKE_RE

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# How long start()/restart() will wait for a configured health_probe to
# report readiness before giving up and reporting LocalMcpState.COLD_START_TIMEOUT.
# Only applies when a health_probe is actually configured -- see module docstring.
DEFAULT_COLD_START_TIMEOUT_SECONDS = 20.0

# With NO health_probe configured, start()/restart() still wait this long
# (much shorter) purely to catch a fast dependency-missing/import-error
# crash before returning -- never long enough to be mistaken for a "wait
# for readiness" window.
DEFAULT_CRASH_SETTLE_SECONDS = 0.5

# Poll cadence for the bounded start()/restart() readiness wait.
DEFAULT_POLL_INTERVAL_SECONDS = 0.05

# How many per-run log files to retain per scope (each start()/restart()
# gets its own file) before pruning the oldest -- bounds disk usage across
# many restarts without needing a full rotation library.
DEFAULT_MAX_LOG_FILES = 10

# tail_log() never reads more than this many bytes off the end of the log
# file, regardless of how large the file itself has grown.
DEFAULT_LOG_TAIL_BYTES = 8000

# doctor()'s log_file check turns "warn" once the current log file exceeds
# this size -- advisory only; tail_log() is bounded regardless.
DEFAULT_LOG_SIZE_WARN_BYTES = 5 * 1024 * 1024

# Fixed lease-broker client name for EVERY LocalRunner instance, regardless
# of which external tool constructed it -- this is what makes the
# process_registry.ProcessLeaseBroker.acquire_exclusive() single-owner
# guardrail actually cross-tool: two different processes (Claude Code,
# Codex, a bare CLI call) racing to start the same scope must compare
# against the SAME client identity, or the broker-level check would never
# see them as the same exclusivity domain (see ProcessLeaseBroker.
# acquire_exclusive's own docstring: exclusivity is scoped to (client,
# owner_key), not owner_key alone).
_LEASE_CLIENT_NAME = "meridian-local-runner"

_STATE_DIR_ENV_VAR = "MERIDIAN_LOCAL_RUNNER_STATE_DIR"

_UNSET = object()


def default_state_dir() -> Path:
    """Where per-scope runner records/logs live -- ``~/.meridian/local_runner``
    unless overridden by ``MERIDIAN_LOCAL_RUNNER_STATE_DIR`` (same override
    convention as ``process_registry.default_registry_path``). Tests always
    override this so they never touch a real home directory."""
    override = os.environ.get(_STATE_DIR_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".meridian" / "local_runner"


_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_scope_slug(scope: str) -> str:
    """Filesystem-safe, collision-resistant slug for *scope*. The human-
    readable prefix aids debugging (``ls ~/.meridian/local_runner``); the
    appended content-hash suffix guarantees two different scopes that
    happen to sanitize to the same slug (e.g. differing only in characters
    the slug strips) never collide on one state file."""
    slug = _SLUG_RE.sub("-", scope).strip("-")[:48] or "scope"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def _scope_state_path(state_dir: Path, scope: str) -> Path:
    return state_dir / f"{_safe_scope_slug(scope)}.json"


def _scope_log_dir(state_dir: Path, scope: str) -> Path:
    return state_dir / "logs" / _safe_scope_slug(scope)


def _atomic_write_json(path: Path, payload: "dict[str, Any]") -> None:
    """Write *payload* to *path* as JSON via temp-file-then-``os.replace``,
    matching ``process_registry.ProcessLeaseBroker._save``'s exact pattern
    so a crash mid-write can never corrupt a reader's view of the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".local_runner_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


def _tail(text: "str | None", limit: int) -> str:
    if not text:
        return ""
    return text[-limit:]


# ---------------------------------------------------------------------------
# Status schema -- child process / local MCP readiness / tunnel readiness,
# deliberately kept as three independent sub-objects (see module docstring
# pillar 1).
# ---------------------------------------------------------------------------


class ChildState(str, Enum):
    NOT_STARTED = "not_started"   # no record for this scope at all
    RUNNING = "running"           # verified alive right now
    STOPPED = "stopped"           # exited cleanly (code 0) or explicitly stopped
    CRASHED = "crashed"           # exited with a non-zero code
    UNKNOWN = "unknown"           # liveness could not be verified (no psutil, no live handle)


class LocalMcpState(str, Enum):
    NOT_CONFIGURED = "not_configured"      # no health_probe was ever supplied
    READY = "ready"                        # health_probe reported ready
    COLD_START_TIMEOUT = "cold_start_timeout"  # probe never succeeded within the bound
    FAILED = "failed"                      # child exited/crashed before ever becoming ready


@dataclass(frozen=True)
class ChildProcessStatus:
    state: ChildState
    pid: "int | None"
    run_id: "str | None"
    create_time: "float | None"
    started_at: "float | None"
    uptime_seconds: "float | None"
    restart_count: int
    exit_code: "int | None"
    last_exit_reason: "str | None"

    def as_dict(self) -> "dict[str, Any]":
        payload = dataclasses.asdict(self)
        payload["state"] = self.state.value
        return payload


@dataclass(frozen=True)
class LocalMcpStatus:
    state: LocalMcpState
    detail: str
    checked_at: "float | None"

    def as_dict(self) -> "dict[str, Any]":
        payload = dataclasses.asdict(self)
        payload["state"] = self.state.value
        return payload


@dataclass(frozen=True)
class TunnelReadinessStatus:
    configured: bool
    label: "str | None"
    state: str
    detail: str

    def as_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class RunnerStatus:
    scope: str
    generated_at: float
    child: ChildProcessStatus
    local_mcp: LocalMcpStatus
    tunnel: TunnelReadinessStatus
    warnings: "tuple[str, ...]" = ()

    def as_dict(self) -> "dict[str, Any]":
        return {
            "scope": self.scope,
            "generated_at": self.generated_at,
            "child": self.child.as_dict(),
            "local_mcp": self.local_mcp.as_dict(),
            "tunnel": self.tunnel.as_dict(),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Doctor schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    severity: str  # "ok" | "warn" | "fail"
    detail: str

    def as_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    scope: str
    generated_at: float
    checks: "tuple[DoctorCheck, ...]"

    @property
    def healthy(self) -> bool:
        return not any(c.severity == "fail" for c in self.checks)

    def as_dict(self) -> "dict[str, Any]":
        return {
            "scope": self.scope,
            "generated_at": self.generated_at,
            "healthy": self.healthy,
            "checks": [c.as_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunnerAlreadyRunningError(RuntimeError):
    """:meth:`LocalRunner.start` refused because a previous record for this
    scope is still verified alive (or unverifiable -- see module docstring
    pillar 2) and ``force`` was not passed. Never raised on account of a
    port scan -- only ever from PID + create_time identity verification."""

    def __init__(self, scope: str, record: "RunnerRecord") -> None:
        self.scope = scope
        self.record = record
        super().__init__(
            f"a local runner for scope {scope!r} appears to already be running "
            f"(pid={record.pid}, run_id={record.run_id!r}, verified via PID+create_time "
            "identity, never by port alone) -- pass force=True to take over, or stop it first."
        )


class ScriptNotAllowedError(ValueError):
    """Raised when a caller asks to run a script name that was never
    declared into a :class:`ScriptAllowlist`, or supplies extra arguments a
    declared spec does not permit. This is the "explicitly scoped" script
    escape-hatch guardrail: there is no code path anywhere in this module
    that executes an arbitrary, caller-supplied command string."""


# ---------------------------------------------------------------------------
# Persisted runner record -- recoverable PID/process-group metadata
# ---------------------------------------------------------------------------


@dataclass
class RunnerRecord:
    """Everything a *different* process needs to recover, verify, and (if
    asked) tear down the child a prior ``LocalRunner.start()``/``restart()``
    call spawned for one scope. Mirrors
    ``process_lifecycle.OwnedProcessHandle``'s own identity fields exactly
    (see :meth:`as_owned_handle`) plus this module's own bookkeeping
    (restart count, log path, local MCP / lease state)."""

    scope: str
    run_id: str
    pid: int
    executable: str
    cwd: "str | None"
    cmdline: "list[str]"
    create_time: "float | None"
    group_id: "int | None"
    job_id: "int | None"
    started_at: float
    restart_count: int = 0
    log_path: "str | None" = None
    tunnel_label: "str | None" = None
    lease_run_id: "str | None" = None
    recovered_from_stale_pid: "int | None" = None
    last_exit_code: "int | None" = None
    last_exit_reason: "str | None" = None
    last_exit_at: "float | None" = None
    local_mcp_state: str = LocalMcpState.NOT_CONFIGURED.value
    local_mcp_detail: str = ""
    local_mcp_checked_at: "float | None" = None

    def to_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "RunnerRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def as_owned_handle(self) -> process_lifecycle.OwnedProcessHandle:
        """Reconstruct the exact ``OwnedProcessHandle`` shape the portable
        lifecycle backends need for a verified, identity-checked close --
        the "recoverable ... process-group metadata" the sprint item asks
        for, applied concretely: a totally different invocation of this
        module can tear down a process it never spawned, using only what
        was persisted here."""
        return process_lifecycle.OwnedProcessHandle(
            run_id=self.run_id,
            pid=self.pid,
            executable=self.executable,
            cwd=self.cwd,
            cmdline=list(self.cmdline),
            create_time=self.create_time,
            group_id=self.group_id,
            job_id=self.job_id,
        )


def _prune_old_logs(log_dir: Path, *, keep: int) -> None:
    try:
        files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Script allowlist / receipts -- the explicitly-scoped script escape hatch
# ---------------------------------------------------------------------------

DEFAULT_SCRIPT_TIMEOUT_SECONDS = 30.0
_SCRIPT_OUTPUT_TAIL_CHARS = 4000

# Broader, NON-anchored variant of capability_manifest._ABSOLUTE_PATH_RE for
# scanning arbitrary free text (a script's captured stdout/stderr) rather
# than validating that an entire field IS a path. capability_manifest's own
# regex is deliberately anchored (`^...`) for that narrower job; reusing it
# here would silently miss a path embedded mid-string (e.g. a traceback
# line like "File \"C:\\Users\\...\\secret_repo\\file.py\", line 42").
_ABSOLUTE_PATH_SCAN_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"'<>|]*|\\\\[^\s\"'<>|]*|/home/[^\s\"'<>|]*|/Users/[^\s\"'<>|]*|/root/[^\s\"'<>|]*)"
)


def _redact_for_shared_state(value: "str | None") -> "str | None":
    """Best-effort textual redaction used by
    :meth:`ScriptReceipt.to_shared_projection`. Reuses
    ``capability_manifest``'s own secret-shaped pattern verbatim (one
    source of truth for "what looks like a secret" across this codebase)
    plus the broader absolute-path scan above. Defense in depth, not a
    guarantee -- never rely on this as the only safeguard against a real
    secret reaching shared Meridian state."""
    if value is None:
        return None
    redacted = _ABSOLUTE_PATH_SCAN_RE.sub("[redacted-local-path]", value)
    redacted = _SECRET_LIKE_RE.sub("[redacted-secret]", redacted)
    return redacted


@dataclass(frozen=True)
class ScriptSpec:
    """One pre-declared, allowlisted command. A :class:`LocalRunner`'s
    script escape hatch can only ever run a script by resolving *name*
    against an already-declared spec -- see :class:`ScriptAllowlist`."""

    name: str
    command: "tuple[str, ...]"
    description: str = ""
    cwd: "str | None" = None
    env: "dict[str, str] | None" = None
    timeout: float = DEFAULT_SCRIPT_TIMEOUT_SECONDS
    allow_extra_args: bool = False

    def to_dict(self) -> "dict[str, Any]":
        return {
            "name": self.name,
            "command": list(self.command),
            "description": self.description,
            "cwd": self.cwd,
            "env": dict(self.env) if self.env else None,
            "timeout": self.timeout,
            "allow_extra_args": self.allow_extra_args,
        }

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "ScriptSpec":
        return cls(
            name=str(data["name"]),
            command=tuple(data["command"]),
            description=str(data.get("description") or ""),
            cwd=data.get("cwd"),
            env=dict(data["env"]) if data.get("env") else None,
            timeout=float(data.get("timeout", DEFAULT_SCRIPT_TIMEOUT_SECONDS)),
            allow_extra_args=bool(data.get("allow_extra_args", False)),
        )


@dataclass(frozen=True)
class ScriptReceipt:
    name: str
    command: "tuple[str, ...]"
    cwd: "str | None"
    started_at: float
    duration_seconds: float
    exit_code: "int | None"
    timed_out: bool
    stdout_tail: str
    stderr_tail: str

    def to_local_dict(self) -> "dict[str, Any]":
        """Full, unredacted detail -- local use only (CLI stdout, a local
        log). Never attach this directly to shared Meridian project state;
        use :meth:`to_shared_projection` for that."""
        return {
            "name": self.name,
            "command": list(self.command),
            "cwd": self.cwd,
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }

    def to_shared_projection(self) -> "dict[str, Any]":
        """Sanitized projection safe to attach to SHARED Meridian project
        state -- see :func:`_redact_for_shared_state`."""
        return {
            "name": self.name,
            "command": [_redact_for_shared_state(tok) for tok in self.command],
            "cwd": _redact_for_shared_state(self.cwd),
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_tail": _redact_for_shared_state(self.stdout_tail),
            "stderr_tail": _redact_for_shared_state(self.stderr_tail),
        }


class ScriptAllowlist:
    """Explicitly-scoped registry of pre-declared scripts. There is no
    code path anywhere in this module that executes an arbitrary,
    caller-supplied command string -- every execution resolves a *name*
    against this allowlist first (see :func:`run_allowlisted_script`)."""

    def __init__(self, specs: "Iterable[ScriptSpec] | None" = None) -> None:
        self._specs: "dict[str, ScriptSpec]" = {}
        for spec in specs or ():
            self.declare(spec)

    def declare(self, spec: ScriptSpec) -> None:
        if not spec.name or not spec.name.strip():
            raise ValueError("script spec must have a non-empty name")
        if not spec.command:
            raise ValueError(f"script {spec.name!r}: command must be non-empty")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ScriptSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise ScriptNotAllowedError(
                f"script {name!r} is not declared in this allowlist -- only "
                f"pre-declared scripts may run: {sorted(self._specs)}"
            )
        return spec

    def names(self) -> "list[str]":
        return sorted(self._specs)

    def to_dict(self) -> "dict[str, Any]":
        return {"scripts": [s.to_dict() for s in self._specs.values()]}

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "ScriptAllowlist":
        return cls(ScriptSpec.from_dict(row) for row in (data.get("scripts") or []))

    @classmethod
    def load(cls, path: "Path | str") -> "ScriptAllowlist":
        """Load a declared-scripts JSON file. A missing or corrupt file
        loads as an empty (i.e. everything refused) allowlist rather than
        raising -- matches ``ProcessLeaseBroker._load``'s own
        fail-safe-empty convention."""
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return cls()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return cls()
        return cls.from_dict(data)

    def save(self, path: "Path | str") -> None:
        _atomic_write_json(Path(path), self.to_dict())


def run_allowlisted_script(
    allowlist: ScriptAllowlist,
    name: str,
    *,
    extra_args: "list[str] | None" = None,
    env: "dict[str, str] | None" = None,
    clock: "Callable[[], float]" = time.time,
) -> ScriptReceipt:
    """Run one declared script by *name*, bounded by its own
    ``spec.timeout``. Raises :class:`ScriptNotAllowedError` for an
    undeclared name or disallowed extra args -- before anything is spawned.
    Output is always tail-bounded (see module docstring pillar 3/4)."""
    spec = allowlist.get(name)
    extra_args = list(extra_args or [])
    if extra_args and not spec.allow_extra_args:
        raise ScriptNotAllowedError(
            f"script {name!r} does not permit extra arguments (allow_extra_args=False)"
        )
    command = list(spec.command) + extra_args
    merged_env = None
    if spec.env or env:
        merged_env = dict(os.environ)
        merged_env.update(spec.env or {})
        merged_env.update(env or {})
    started_at = clock()
    start_monotonic = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=spec.cwd,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=spec.timeout,
            check=False,
        )
        duration = time.monotonic() - start_monotonic
        return ScriptReceipt(
            name=name,
            command=tuple(command),
            cwd=spec.cwd,
            started_at=started_at,
            duration_seconds=duration,
            exit_code=proc.returncode,
            timed_out=False,
            stdout_tail=_tail(proc.stdout, _SCRIPT_OUTPUT_TAIL_CHARS),
            stderr_tail=_tail(proc.stderr, _SCRIPT_OUTPUT_TAIL_CHARS),
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start_monotonic
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return ScriptReceipt(
            name=name,
            command=tuple(command),
            cwd=spec.cwd,
            started_at=started_at,
            duration_seconds=duration,
            exit_code=None,
            timed_out=True,
            stdout_tail=_tail(out, _SCRIPT_OUTPUT_TAIL_CHARS),
            stderr_tail=_tail(err, _SCRIPT_OUTPUT_TAIL_CHARS),
        )


# ---------------------------------------------------------------------------
# LocalRunner
# ---------------------------------------------------------------------------


class LocalRunner:
    """Local-first supervisor for one scoped child process. See module
    docstring for the full design contract."""

    def __init__(
        self,
        scope: str,
        command: "Sequence[str] | None",
        *,
        cwd: "str | None" = None,
        env: "dict[str, str] | None" = None,
        state_dir: "Path | None" = None,
        backend: "Any | None" = None,
        broker: "process_registry.ProcessLeaseBroker | None" = _UNSET,  # type: ignore[assignment]
        clock: "Callable[[], float]" = time.time,
        health_probe: "Callable[[], bool] | None" = None,
        tunnel_label: "str | None" = None,
        cold_start_timeout: float = DEFAULT_COLD_START_TIMEOUT_SECONDS,
        crash_settle_seconds: float = DEFAULT_CRASH_SETTLE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_log_files: int = DEFAULT_MAX_LOG_FILES,
    ) -> None:
        """*command* may be ``None`` for a "reconnect to an existing scope"
        instance used for read-only/recovery operations (``status``,
        ``doctor``, ``stop``, ``tail_log``) or for ``restart``/``preflight``,
        which both recover the last-recorded command from the persisted
        :class:`RunnerRecord` when *command* is not supplied (see
        :meth:`_resolve_command_for_readonly_op`). :meth:`start` requires a
        real *command*.

        *broker* defaults to the process-wide
        ``process_registry.get_broker()`` singleton; pass ``broker=None``
        explicitly to disable cross-tool lease registration entirely (tests
        should always pass an explicit broker or ``None`` -- never rely on
        the real, home-directory-backed default).
        """
        if not scope or not scope.strip():
            raise ValueError("scope must be a non-empty string")
        self.scope = scope
        self.command = list(command) if command else None
        self.cwd = cwd
        self.env = env
        self._state_dir = state_dir or default_state_dir()
        self._state_path = _scope_state_path(self._state_dir, scope)
        self._backend = backend or process_lifecycle.get_default_backend()
        self._broker = process_registry.get_broker() if broker is _UNSET else broker
        self._clock = clock
        self.health_probe = health_probe
        self.tunnel_label = tunnel_label
        self.cold_start_timeout = cold_start_timeout
        self.crash_settle_seconds = crash_settle_seconds
        self.poll_interval = poll_interval
        self.max_log_files = max_log_files
        self._owner_key = f"local-runner:{scope}"
        self._live_handle: "process_lifecycle.OwnedProcessHandle | None" = None

    def __enter__(self) -> "LocalRunner":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 -- cleanup must never mask the real exception
            pass

    # -- persistence ------------------------------------------------------

    def _load_record(self) -> "RunnerRecord | None":
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None
        if not data:
            return None
        try:
            return RunnerRecord.from_dict(data)
        except Exception:  # noqa: BLE001 -- a corrupt/foreign record must not crash a read
            return None

    def _save_record(self, record: RunnerRecord) -> None:
        _atomic_write_json(self._state_path, record.to_dict())

    def _clear_record(self) -> None:
        try:
            self._state_path.unlink()
        except OSError:
            pass

    # -- liveness -----------------------------------------------------------

    def _is_pid_alive(self, record: RunnerRecord) -> "bool | None":
        """Tri-state liveness check: ``True`` (confirmed running), ``False``
        (confirmed gone -- safe to recover), or ``None`` (unverifiable).

        Deliberately distinct from ``process_lifecycle.verify_handle_live``,
        which degrades to ``True`` when uncertain (the right call for "is it
        still safe to assume this is the same process I shouldn't kill");
        this module wants an HONEST tri-state for status reporting instead
        of a safety-biased boolean, so an unverifiable record reports
        ``unknown`` rather than a possibly-false ``running``.
        """
        if (
            self._live_handle is not None
            and self._live_handle.run_id == record.run_id
            and self._live_handle.popen is not None
        ):
            return self._live_handle.popen.poll() is None
        try:
            import psutil  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        try:
            if not psutil.pid_exists(record.pid):
                return False
            if record.create_time is not None:
                proc = psutil.Process(record.pid)
                return abs(proc.create_time() - float(record.create_time)) < 1.0
            return True
        except Exception:  # noqa: BLE001
            return False

    def _try_recover_exit_code(self, record: RunnerRecord) -> "int | None":
        if (
            self._live_handle is not None
            and self._live_handle.run_id == record.run_id
            and self._live_handle.popen is not None
        ):
            return self._live_handle.popen.poll()
        return None

    def _poll_live_handle(self) -> "int | None":
        if self._live_handle is None or self._live_handle.popen is None:
            return None
        return self._live_handle.popen.poll()

    # -- lease broker (cross-tool visibility; best-effort, never blocking) --

    def _acquire_lease(
        self, handle: process_lifecycle.OwnedProcessHandle, *, force: bool
    ) -> "str | None":
        if self._broker is None:
            return None
        try:
            lease = self._broker.acquire_exclusive(
                _LEASE_CLIENT_NAME,
                self._owner_key,
                handle.pid,
                executable=handle.executable,
                cwd=handle.cwd,
                cmdline=list(handle.cmdline),
                create_time=handle.create_time,
                group_id=handle.group_id,
                job_id=handle.job_id,
                force=force,
            )
            return lease.run_id
        except process_registry.OwnerConflictError:
            # Best-effort cross-tool visibility only -- this module's own
            # on-disk record already gated the real exclusivity decision
            # before spawning; a conflicting broker-level lease from a
            # DIFFERENT tracking mechanism is surfaced for visibility, not
            # treated as fatal (the child has already been spawned by now).
            return None
        except Exception:  # noqa: BLE001 -- leasing must never break a working spawn
            return None

    def _release_lease(self, record: RunnerRecord) -> None:
        if self._broker is None or not record.lease_run_id:
            return
        try:
            self._broker.release(_LEASE_CLIENT_NAME, record.lease_run_id)
        except Exception:  # noqa: BLE001 -- best-effort
            pass

    # -- command/cwd recovery for read-only / restart / preflight ops ------

    def _resolve_command_for_readonly_op(self) -> "tuple[str, ...]":
        if self.command:
            return tuple(self.command)
        record = self._load_record()
        if record is not None and record.cmdline:
            return tuple(record.cmdline)
        raise ValueError(
            f"local runner scope {self.scope!r}: no command was supplied and no "
            "prior run was recorded to recover one from"
        )

    def _resolve_cwd_for_readonly_op(self, record: "RunnerRecord | None") -> "str | None":
        if self.cwd is not None:
            return self.cwd
        return record.cwd if record is not None else None

    # -- spawn / terminate --------------------------------------------------

    def _prepare_log_path(self) -> Path:
        log_dir = _scope_log_dir(self._state_dir, self.scope)
        log_dir.mkdir(parents=True, exist_ok=True)
        token = process_lifecycle.new_run_id()
        return log_dir / f"{token}.log"

    def _spawn(
        self, command: "Sequence[str]", cwd: "str | None", log_path: Path
    ) -> process_lifecycle.OwnedProcessHandle:
        fh = open(log_path, "ab", buffering=0)
        try:
            handle = self._backend.spawn(
                list(command),
                env=self.env,
                cwd=cwd,
                popen_kwargs={"stdout": fh, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL},
            )
        finally:
            fh.close()
        _prune_old_logs(log_path.parent, keep=self.max_log_files)
        return handle

    def _terminate(self, record: RunnerRecord) -> bool:
        """Best-effort graceful-then-forced close of *record*'s owned
        process (and its lease). Always identity-verified via
        ``process_lifecycle.verify_handle_live`` inside the backend's own
        ``close()`` -- a record whose PID has since been reused by an
        unrelated process is never signalled. Safe to call repeatedly
        (idempotent) even from a process that never held a live handle."""
        handle = record.as_owned_handle()
        if self._live_handle is not None and self._live_handle.run_id == record.run_id:
            handle = self._live_handle
        ok = self._backend.close(handle)
        self._release_lease(record)
        if self._live_handle is not None and self._live_handle.run_id == record.run_id:
            self._live_handle = None
        return ok

    def _await_readiness(self, record: RunnerRecord) -> None:
        """Bounded post-spawn wait -- see module docstring pillar 3. Always
        checks for a fast crash; additionally polls ``health_probe`` (if
        configured) up to ``cold_start_timeout``. Mutates and persists
        *record* in place; never raises (a broken probe degrades to "not
        ready yet", not a crashed start())."""
        has_probe = self.health_probe is not None
        deadline = self._clock() + (self.cold_start_timeout if has_probe else self.crash_settle_seconds)
        while True:
            exit_code = self._poll_live_handle()
            if exit_code is not None:
                record.last_exit_code = exit_code
                record.last_exit_reason = "crashed" if exit_code != 0 else "exited"
                record.last_exit_at = self._clock()
                if has_probe:
                    record.local_mcp_state = LocalMcpState.FAILED.value
                    record.local_mcp_detail = (
                        f"child process exited (code {exit_code}) before local MCP became ready"
                    )
                    record.local_mcp_checked_at = self._clock()
                self._save_record(record)
                return
            if has_probe:
                try:
                    ready = bool(self.health_probe())
                except Exception:  # noqa: BLE001 -- a broken probe must never crash start()
                    ready = False
                if ready:
                    record.local_mcp_state = LocalMcpState.READY.value
                    record.local_mcp_detail = "health probe reported ready"
                    record.local_mcp_checked_at = self._clock()
                    self._save_record(record)
                    return
            if self._clock() >= deadline:
                break
            time.sleep(self.poll_interval)
        if has_probe:
            record.local_mcp_state = LocalMcpState.COLD_START_TIMEOUT.value
            record.local_mcp_detail = (
                f"health probe did not report ready within {self.cold_start_timeout:.1f}s of cold start"
            )
            record.local_mcp_checked_at = self._clock()
        self._save_record(record)

    def _start_internal(
        self,
        *,
        command: "Sequence[str]",
        cwd: "str | None",
        restart_count: int,
        recovered_from_pid: "int | None" = None,
        force_lease: bool = False,
    ) -> RunnerStatus:
        log_path = self._prepare_log_path()
        handle = self._spawn(command, cwd, log_path)
        self._live_handle = handle
        lease_run_id = self._acquire_lease(handle, force=force_lease)
        record = RunnerRecord(
            scope=self.scope,
            run_id=handle.run_id,
            pid=handle.pid,
            executable=handle.executable,
            cwd=handle.cwd,
            cmdline=list(handle.cmdline),
            create_time=handle.create_time,
            group_id=handle.group_id,
            job_id=handle.job_id,
            started_at=self._clock(),
            restart_count=restart_count,
            log_path=str(log_path),
            tunnel_label=self.tunnel_label,
            lease_run_id=lease_run_id,
            recovered_from_stale_pid=recovered_from_pid,
        )
        self._save_record(record)
        self._await_readiness(record)
        return self.status()

    # -- public operations ---------------------------------------------------

    def start(self, *, force: bool = False) -> RunnerStatus:
        """Spawn a fresh child for this scope. Refuses
        (:class:`RunnerAlreadyRunningError`) if a prior record is still
        verified alive (or unverifiable) unless ``force=True``. A prior
        record CONFIRMED gone is recovered automatically -- no ``force``
        needed (see module docstring pillar 2)."""
        if not self.command:
            raise ValueError(f"local runner scope {self.scope!r}: start() requires a command")
        existing = self._load_record()
        recovered_from_pid: "int | None" = None
        restart_count = 0
        force_lease = False
        if existing is not None:
            alive = self._is_pid_alive(existing)
            if alive is not False:  # True (running) or None (unverifiable) -> block unless forced
                if not force:
                    raise RunnerAlreadyRunningError(self.scope, existing)
                self._terminate(existing)
                force_lease = True
            else:
                recovered_from_pid = existing.pid
                restart_count = existing.restart_count
        return self._start_internal(
            command=self.command,
            cwd=self.cwd,
            restart_count=restart_count,
            recovered_from_pid=recovered_from_pid,
            force_lease=force_lease,
        )

    def stop(self) -> RunnerStatus:
        """Gracefully (then forcibly) stop this scope's child, if any.
        Idempotent -- safe to call when nothing is running."""
        record = self._load_record()
        if record is None:
            return self.status()
        ok = self._terminate(record)
        record.last_exit_code = 0 if ok else record.last_exit_code
        record.last_exit_reason = "stopped" if ok else "stop_unconfirmed"
        record.last_exit_at = self._clock()
        self._save_record(record)
        return self.status()

    def restart(self) -> RunnerStatus:
        """Stop (if running) then respawn, unconditionally -- restart never
        raises :class:`RunnerAlreadyRunningError`, unlike :meth:`start`.
        Recovers the command/cwd from the last persisted record when this
        instance was constructed with ``command=None`` (see
        :meth:`_resolve_command_for_readonly_op`)."""
        existing = self._load_record()
        command = self._resolve_command_for_readonly_op()
        cwd = self._resolve_cwd_for_readonly_op(existing)
        restart_count = (existing.restart_count + 1) if existing is not None else 0
        if existing is not None:
            self._terminate(existing)
        return self._start_internal(
            command=command, cwd=cwd, restart_count=restart_count, force_lease=True,
        )

    def status(self) -> RunnerStatus:
        """Bounded, read-only status snapshot -- one state-file read plus
        one liveness check, no sleeping or looping (see module docstring
        pillar 3)."""
        record = self._load_record()
        child = self._build_child_status(record)
        local_mcp = self._build_local_mcp_status(record, child)
        tunnel = self._build_tunnel_status()
        warnings: "list[str]" = []
        if record is not None and record.recovered_from_stale_pid is not None:
            warnings.append(
                f"recovered from a stale record (previous pid "
                f"{record.recovered_from_stale_pid} was no longer running)"
            )
        return RunnerStatus(
            scope=self.scope,
            generated_at=self._clock(),
            child=child,
            local_mcp=local_mcp,
            tunnel=tunnel,
            warnings=tuple(warnings),
        )

    def _build_child_status(self, record: "RunnerRecord | None") -> ChildProcessStatus:
        if record is None:
            return ChildProcessStatus(
                state=ChildState.NOT_STARTED, pid=None, run_id=None, create_time=None,
                started_at=None, uptime_seconds=None, restart_count=0, exit_code=None,
                last_exit_reason=None,
            )
        alive = self._is_pid_alive(record)
        exit_code = record.last_exit_code
        reason = record.last_exit_reason
        uptime: "float | None" = None
        if alive is True:
            state = ChildState.RUNNING
            uptime = max(0.0, self._clock() - record.started_at)
            exit_code = None
            reason = None
        elif alive is False:
            if exit_code is None:
                recovered = self._try_recover_exit_code(record)
                if recovered is not None:
                    exit_code = recovered
                    reason = "crashed" if recovered != 0 else "exited"
                    record.last_exit_code = exit_code
                    record.last_exit_reason = reason
                    record.last_exit_at = self._clock()
                    self._save_record(record)
                else:
                    reason = reason or "process is no longer running (exit code unavailable from this invocation)"
            state = ChildState.CRASHED if (exit_code is not None and exit_code != 0) else ChildState.STOPPED
        else:
            state = ChildState.UNKNOWN
        return ChildProcessStatus(
            state=state, pid=record.pid, run_id=record.run_id, create_time=record.create_time,
            started_at=record.started_at, uptime_seconds=uptime, restart_count=record.restart_count,
            exit_code=exit_code, last_exit_reason=reason,
        )

    def _build_local_mcp_status(
        self, record: "RunnerRecord | None", child: ChildProcessStatus
    ) -> LocalMcpStatus:
        if record is None:
            return LocalMcpStatus(state=LocalMcpState.NOT_CONFIGURED, detail="", checked_at=None)
        state_value = record.local_mcp_state
        detail = record.local_mcp_detail
        checked_at = record.local_mcp_checked_at
        if (
            child.state in (ChildState.CRASHED, ChildState.STOPPED, ChildState.NOT_STARTED)
            and state_value == LocalMcpState.READY.value
        ):
            state_value = LocalMcpState.FAILED.value
            detail = "child process is not running"
        try:
            state = LocalMcpState(state_value)
        except ValueError:
            state = LocalMcpState.NOT_CONFIGURED
        return LocalMcpStatus(state=state, detail=detail, checked_at=checked_at)

    def _build_tunnel_status(self) -> TunnelReadinessStatus:
        if not self.tunnel_label:
            return TunnelReadinessStatus(
                configured=False, label=None, state="not_configured",
                detail="no tunnel_label configured for this runner",
            )
        lc = tunnel_lifecycle.get_lifecycle(self.tunnel_label)
        snap = lc.snapshot()
        detail = snap["history"][-1]["detail"] if snap["history"] else ""
        return TunnelReadinessStatus(
            configured=True, label=self.tunnel_label, state=snap["state"], detail=detail,
        )

    def doctor(self) -> DoctorReport:
        """Bounded diagnostic sweep -- every individual check is itself
        bounded (a state-dir write probe, a lease-broker list call, a
        single ``status()`` snapshot); nothing here loops or waits on the
        child."""
        status = self.status()
        checks = (
            self._doctor_state_dir_writable(),
            self._doctor_lease_broker(),
            self._doctor_executable(),
            self._doctor_child(status.child),
            self._doctor_local_mcp(status.local_mcp),
            self._doctor_tunnel(status.tunnel),
            self._doctor_log_file(),
        )
        return DoctorReport(scope=self.scope, generated_at=self._clock(), checks=checks)

    def _doctor_state_dir_writable(self) -> DoctorCheck:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            probe = self._state_dir / f".doctor-probe-{os.getpid()}.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return DoctorCheck("state_dir_writable", "ok", str(self._state_dir))
        except OSError as exc:
            return DoctorCheck("state_dir_writable", "fail", f"cannot write to {self._state_dir}: {exc}")

    def _doctor_lease_broker(self) -> DoctorCheck:
        if self._broker is None:
            return DoctorCheck(
                "lease_broker", "warn", "no lease broker configured -- cross-tool visibility disabled",
            )
        try:
            self._broker.list_leases(client=_LEASE_CLIENT_NAME)
            return DoctorCheck("lease_broker", "ok", "reachable")
        except Exception as exc:  # noqa: BLE001
            return DoctorCheck("lease_broker", "warn", f"lease broker unavailable: {exc}")

    def _doctor_executable(self) -> DoctorCheck:
        try:
            command = self._resolve_command_for_readonly_op()
        except ValueError as exc:
            return DoctorCheck("executable_resolves", "warn", str(exc))
        resolved = tunnel_preflight.resolve_effective_executable(command)
        launcher = command[0]
        found = bool(shutil.which(launcher)) or (os.path.isabs(launcher) and os.path.exists(launcher))
        if resolved and found:
            return DoctorCheck("executable_resolves", "ok", resolved)
        return DoctorCheck("executable_resolves", "fail", f"cannot resolve launcher {launcher!r} on PATH")

    def _doctor_child(self, child: ChildProcessStatus) -> DoctorCheck:
        if child.state in (ChildState.RUNNING, ChildState.NOT_STARTED, ChildState.STOPPED):
            return DoctorCheck("child_process", "ok", child.state.value)
        if child.state is ChildState.UNKNOWN:
            return DoctorCheck(
                "child_process", "warn",
                "liveness could not be verified (psutil unavailable and no in-process handle)",
            )
        return DoctorCheck("child_process", "fail", f"child process crashed (exit code {child.exit_code})")

    def _doctor_local_mcp(self, local_mcp: LocalMcpStatus) -> DoctorCheck:
        if local_mcp.state in (LocalMcpState.READY, LocalMcpState.NOT_CONFIGURED):
            return DoctorCheck("local_mcp_readiness", "ok", local_mcp.detail or local_mcp.state.value)
        if local_mcp.state is LocalMcpState.COLD_START_TIMEOUT:
            return DoctorCheck("local_mcp_readiness", "warn", local_mcp.detail)
        return DoctorCheck(
            "local_mcp_readiness", "fail", local_mcp.detail or "local MCP endpoint failed to become ready",
        )

    def _doctor_tunnel(self, tunnel: TunnelReadinessStatus) -> DoctorCheck:
        if not tunnel.configured:
            return DoctorCheck("tunnel_readiness", "ok", "no tunnel configured for this runner")
        if tunnel.state == tunnel_lifecycle.LifecycleState.READY.value:
            return DoctorCheck("tunnel_readiness", "ok", tunnel.detail or "ready")
        if tunnel.state in (
            tunnel_lifecycle.LifecycleState.NEVER_READY.value,
            tunnel_lifecycle.LifecycleState.CLIENT_LOST.value,
        ):
            return DoctorCheck("tunnel_readiness", "fail", tunnel.detail or tunnel.state)
        return DoctorCheck("tunnel_readiness", "warn", tunnel.detail or tunnel.state)

    def _doctor_log_file(self) -> DoctorCheck:
        record = self._load_record()
        if record is None or not record.log_path:
            return DoctorCheck("log_file", "ok", "no log file yet")
        path = Path(record.log_path)
        if not path.exists():
            return DoctorCheck("log_file", "warn", f"log file missing: {path}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            return DoctorCheck("log_file", "warn", f"cannot stat log file: {exc}")
        if size > DEFAULT_LOG_SIZE_WARN_BYTES:
            return DoctorCheck("log_file", "warn", f"log file is {size} bytes (over {DEFAULT_LOG_SIZE_WARN_BYTES})")
        return DoctorCheck("log_file", "ok", f"{size} bytes")

    def preflight(self, *, timeout: "float | None" = None) -> tunnel_preflight.PreflightDiagnostic:
        """Isolated launch check for this scope's command -- delegates
        entirely to ``tunnel_preflight`` (single source of truth for
        launch-failure classification) rather than duplicating it. Recovers
        the command from the last persisted record when this instance was
        constructed with ``command=None``."""
        command = self._resolve_command_for_readonly_op()
        record = self._load_record()
        cwd = self._resolve_cwd_for_readonly_op(record)
        if timeout is not None:
            return tunnel_preflight.preflight_child_entrypoint(
                self.scope, command, cwd=cwd, env=self.env, timeout=timeout,
            )
        return tunnel_preflight.preflight_for_label(self.scope, command, cwd=cwd, env=self.env)

    def tail_log(self, *, max_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> str:
        """Bounded tail of the current/last log file -- never reads more
        than *max_bytes* off the end of the file regardless of how large it
        has grown (see module docstring pillar 3)."""
        record = self._load_record()
        if record is None or not record.log_path:
            return ""
        path = Path(record.log_path)
        if not path.exists():
            return ""
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                data = fh.read()
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# CLI -- scriptable JSON controls (mirrors process_registry.py's own
# client-neutral CLI contract exactly: one JSON object per command on
# stdout, exit 0 on success / 1 on a protocol error with {"error": ...} on
# stderr).
# ---------------------------------------------------------------------------


_CMD_HELP = (
    "Command to run, as trailing arguments -- MUST come after every other "
    "flag on this subcommand (argparse.REMAINDER swallows everything after "
    "it starts matching, dashes included, e.g. `... start --scope x -- "
    "python -m meridian --mcp`)."
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.local_runner",
        description=(
            "Local-first Meridian runner: status/doctor/preflight/restart/"
            "log-tail with scriptable JSON controls (899936dd)."
        ),
    )
    parser.add_argument(
        "--state-dir", default=None,
        help="Override the runner state directory (defaults to "
        "MERIDIAN_LOCAL_RUNNER_STATE_DIR or ~/.meridian/local_runner).",
    )
    parser.add_argument(
        "--scripts-file", default=None,
        help="Declared-scripts JSON file for list-scripts/run-script "
        "(defaults to <state-dir>/scripts.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Spawn a new local runner child for --scope.")
    start.add_argument("--scope", required=True)
    start.add_argument("--cwd", default=None)
    start.add_argument("--force", action="store_true")
    start.add_argument("--cold-start-timeout", type=float, default=DEFAULT_COLD_START_TIMEOUT_SECONDS)
    start.add_argument("--tunnel-label", default=None)
    start.add_argument("cmd", nargs=argparse.REMAINDER, help=_CMD_HELP)

    stop = sub.add_parser("stop", help="Gracefully stop the runner for --scope.")
    stop.add_argument("--scope", required=True)

    restart = sub.add_parser("restart", help="Stop then respawn using the last recorded command for --scope.")
    restart.add_argument("--scope", required=True)
    restart.add_argument("--cwd", default=None)
    restart.add_argument("cmd", nargs=argparse.REMAINDER, help=_CMD_HELP + " Omit to reuse the last recorded command.")

    status = sub.add_parser("status", help="Bounded status snapshot for --scope.")
    status.add_argument("--scope", required=True)
    status.add_argument("--tunnel-label", default=None)

    doctor = sub.add_parser("doctor", help="Bounded diagnostic checks for --scope.")
    doctor.add_argument("--scope", required=True)
    doctor.add_argument("--tunnel-label", default=None)
    doctor.add_argument("cmd", nargs=argparse.REMAINDER, help=_CMD_HELP + " Omit to reuse the last recorded command.")

    preflight = sub.add_parser(
        "preflight", help="Isolated launch check for --scope's command, before relying on it.",
    )
    preflight.add_argument("--scope", required=True)
    preflight.add_argument("--cwd", default=None)
    preflight.add_argument("--timeout", type=float, default=None)
    preflight.add_argument("cmd", nargs=argparse.REMAINDER, help=_CMD_HELP + " Omit to reuse the last recorded command.")

    tail = sub.add_parser("tail-log", help="Bounded tail of the last recorded log for --scope.")
    tail.add_argument("--scope", required=True)
    tail.add_argument("--max-bytes", type=int, default=DEFAULT_LOG_TAIL_BYTES)

    sub.add_parser("list-scripts", help="List declared, allowlisted script names.")

    run_script = sub.add_parser("run-script", help="Run one declared, allowlisted script by name.")
    run_script.add_argument("--name", required=True)
    run_script.add_argument("--arg", action="append", dest="extra_args", default=None)
    run_script.add_argument(
        "--shared", action="store_true",
        help="Print the redacted shared-state projection instead of the full local receipt.",
    )

    return parser


def _state_dir_from_args(args: argparse.Namespace) -> "Path | None":
    return Path(args.state_dir) if args.state_dir else None


def _normalize_cmd_arg(cmd: "list[str] | None") -> "list[str] | None":
    """argparse.REMAINDER includes a literal leading ``--`` token when the
    caller used one to separate flags from the command (a documented
    argparse wart) -- strip it so ``-- python -m meridian --mcp`` and
    ``python -m meridian --mcp`` behave identically."""
    if not cmd:
        return None
    if cmd[0] == "--":
        cmd = cmd[1:]
    return cmd or None


def _runner_from_args(args: argparse.Namespace) -> LocalRunner:
    return LocalRunner(
        scope=args.scope,
        command=_normalize_cmd_arg(getattr(args, "cmd", None)),
        cwd=getattr(args, "cwd", None),
        state_dir=_state_dir_from_args(args),
        tunnel_label=getattr(args, "tunnel_label", None),
        cold_start_timeout=getattr(args, "cold_start_timeout", DEFAULT_COLD_START_TIMEOUT_SECONDS),
    )


def _scripts_path_from_args(args: argparse.Namespace) -> Path:
    if args.scripts_file:
        return Path(args.scripts_file)
    state_dir = _state_dir_from_args(args) or default_state_dir()
    return state_dir / "scripts.json"


def main(argv: "list[str] | None" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "start":
            result = _runner_from_args(args).start(force=args.force).as_dict()
        elif args.command == "stop":
            runner = LocalRunner(
                scope=args.scope, command=None, state_dir=_state_dir_from_args(args),
            )
            result = runner.stop().as_dict()
        elif args.command == "restart":
            result = _runner_from_args(args).restart().as_dict()
        elif args.command == "status":
            result = _runner_from_args(args).status().as_dict()
        elif args.command == "doctor":
            result = _runner_from_args(args).doctor().as_dict()
        elif args.command == "preflight":
            diagnostic = _runner_from_args(args).preflight(timeout=args.timeout)
            result = diagnostic.as_dict()
        elif args.command == "tail-log":
            runner = LocalRunner(
                scope=args.scope, command=None, state_dir=_state_dir_from_args(args),
            )
            result = {"scope": args.scope, "log_tail": runner.tail_log(max_bytes=args.max_bytes)}
        elif args.command == "list-scripts":
            allowlist = ScriptAllowlist.load(_scripts_path_from_args(args))
            result = {"scripts": allowlist.names()}
        elif args.command == "run-script":
            allowlist = ScriptAllowlist.load(_scripts_path_from_args(args))
            receipt = run_allowlisted_script(allowlist, args.name, extra_args=args.extra_args)
            result = receipt.to_shared_projection() if args.shared else receipt.to_local_dict()
        else:  # pragma: no cover -- argparse `required=True` prevents this
            parser.error(f"unknown command {args.command!r}")
            return 2
    except (RunnerAlreadyRunningError, ScriptNotAllowedError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised via main() in tests
    sys.exit(main())
