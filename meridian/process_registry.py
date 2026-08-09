"""315b0a63 — cross-client worker-lease broker.

``meridian/process_lifecycle.py`` gives Meridian a portable way to OWN a
process tree it itself spawns (Windows Job Objects / POSIX process groups).
That covers processes Meridian launches directly — tunnel proxy slots,
Serena daemons. It does **not** cover the boundary this module exists for,
confirmed 2026-08-04: Claude Code, Codex, and Claude Desktop routinely
launch MCP servers or subagents of their OWN, entirely outside Meridian's
process tree. Meridian cannot discover, let alone safely clean up, a
process it never spawned and has no identity for — name-based matching
(``_TARGET_NAME_SUBSTRINGS`` in ``orphan_reaper.py``) is exactly the
unsafe shortcut this item's notes rule out.

This module defines a small, **client-neutral lease protocol** instead:
any external client that spawns a worker/MCP-server process it wants
Meridian to be aware of calls :meth:`ProcessLeaseBroker.register` (directly
in-process, via the CLI wrapper at the bottom of this file, or — for a
self-hosted install whose FastAPI process shares a machine with the
client — the ``get_tunnel_diagnostics`` MCP tool's new ``process_leases``
summary, wired in ``routes/tunnel.py``). Registration hands back a
``run_id`` the client re-presents on every subsequent call:

* :meth:`heartbeat` — proves the client is still alive; refreshes the
  lease's expiry.
* :meth:`release` — explicit, graceful release on normal client stop.
* :meth:`sweep_expired` — expiry recovery: a lease whose heartbeat has
  lapsed past its TTL is expired. This does **not** kill anything — see
  below.
* :meth:`report_unowned_survivors` — of the expired leases, the ones whose
  OS process (verified via ``process_lifecycle.verify_handle_live``,
  the same create_time / PID-reuse guard used elsewhere) is STILL alive.
  These are the interesting case: the owning client crashed without
  releasing, and its child process survived it.

The broker is deliberately conservative about destruction: it **never**
kills a process itself, and :meth:`request_cleanup` — the one
lease-mutating call intended to gate an actual kill a caller performs
separately — refuses (raises :class:`ForeignLeaseError`) unless the
requesting client is the SAME client that registered the lease. This is
the sprint notes' three guardrails in one mechanism:

* "refuse unregistered destructive cleanup" — no ``run_id``, no
  ``client``, no cleanup authorization, full stop (:class:`LeaseNotFoundError`).
* "preserve peer leases" / "no-cross-session-kill" — a DIFFERENT client
  holding a DIFFERENT lease is never touched by another client's release
  or cleanup call, even accidentally (:class:`ForeignLeaseError`).
* "report unowned survivors" — a crashed client's orphaned child is
  surfaced for a human/operator decision, never auto-killed by the broker.

Shared runtime reference counting (:meth:`acquire_shared_runtime` /
:meth:`release_shared_runtime`) lets several independent clients agree to
share ONE long-lived runtime (the sprint notes' example: a single Serena
daemon serving Claude Code, Codex, AND Claude Desktop at once) without
each spawning its own duplicate — the caller that actually owns the
daemon's lifecycle asks :meth:`should_close_shared_runtime` before tearing
it down, and only closes it once every holder has released.

Persistence is a single small JSON file (default
``~/.meridian/process_leases.json``, overridable via the
``MERIDIAN_LEASE_REGISTRY_PATH`` env var — tests always override this so
they never touch a real home directory) written with an atomic
temp-file-then-``os.replace`` so a crash mid-write can never corrupt the
registry a concurrent reader depends on. This is intentionally NOT the
Meridian SQLite/Postgres DB: the whole point of this module is a
lightweight, dependency-free, **local-machine** contract any external
process (not just Meridian's own Python code) can speak, including
directly from a shell via the CLI wrapper at the bottom of this file
(``python -m meridian.process_registry register --client codex --pid 1234``).

Deliberately out of scope (per the sprint notes' final sentence): opaque
vendor-internal subagents this module has no PID/lease visibility into at
all. The broker only ever knows about a process because SOMETHING called
:meth:`register` for it — it never guesses.

c5c3fc5f — agent/subprocess execution-event capture
------------------------------------------------------
:func:`register_process` / :func:`release_process` are an ADDITIVE async
layer on top of :meth:`ProcessLeaseBroker.register` / :meth:`.release`
(both left completely unchanged, still synchronous, still with zero DB
dependency) that ALSO best-effort records an ``agent.registered`` /
``agent.released`` :class:`meridian.ai_log.ExecutionEvent` via an injected
``capture`` callable — see :mod:`meridian.session_tools`'s
``capture_process_registered``/``capture_process_released`` for the actual
event shape. This module still never imports ``meridian.db`` or
``meridian.ai_log`` directly (dependency injection keeps the lease broker's
own dependency-free contract intact); a caller that has no DB/project
context simply omits ``capture`` and gets the exact pre-existing
synchronous behavior.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import process_lifecycle as _process_lifecycle

# Default lease time-to-live: how long a lease survives without a heartbeat
# before it's considered expired. Generous enough that a normal MCP-client
# heartbeat cadence (seconds-to-low-minutes) never races it, short enough
# that a genuinely crashed client's orphan is detected promptly.
DEFAULT_TTL_SECONDS = 90.0

# Env var override for the persisted registry file's location. Tests set
# this to a tmp_path so they never read/write a real home directory.
_REGISTRY_PATH_ENV_VAR = "MERIDIAN_LEASE_REGISTRY_PATH"


def default_registry_path() -> Path:
    """Where the persisted lease registry lives — ``~/.meridian/process_leases.json``
    unless overridden by ``MERIDIAN_LEASE_REGISTRY_PATH`` (same override
    pattern as ``pixi_env_retention.default_detached_environments_root``:
    a real, portable default via ``Path.home()``, never hard-coded)."""
    override = os.environ.get(_REGISTRY_PATH_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".meridian" / "process_leases.json"


class LeaseNotFoundError(KeyError):
    """No live (non-released) lease is registered for the given run_id."""


class ForeignLeaseError(PermissionError):
    """A client attempted a mutating/destructive operation (heartbeat,
    release, cleanup) on a lease it did not register. This is the
    "preserve peer leases" / "no-cross-session-kill" guardrail — the
    broker refuses outright rather than guessing whose lease "probably"
    belongs to whom."""


class OwnerConflictError(RuntimeError):
    """39c8cf2c — :meth:`ProcessLeaseBroker.acquire_exclusive` refused
    because a DIFFERENT, VERIFIED-STILL-LIVE lease already holds the same
    ``(client, owner_key)`` identity. This is the single-owner-lease
    guardrail the tunnel-restart-lifecycle sprint item asks for: a second
    wrapper process starting up while an earlier one is genuinely still
    running (not just a process that never got its PID reused) must not
    silently coexist with it — the incident this item is fixing was
    exactly "the old wrapper remained alive with an established network
    connection" alongside a NEW one that had already reported itself
    connected.

    The broker never kills anything to resolve this — see the module
    docstring's "deliberately conservative about destruction" note. The
    caller decides: report and exit, prompt for an explicit ``force=True``
    takeover, or something else appropriate to the calling context.
    """

    def __init__(self, client: str, owner_key: str, lease: "WorkerLease") -> None:
        self.client = client
        self.owner_key = owner_key
        self.lease = lease
        super().__init__(
            f"client {client!r} already holds a live exclusive lease for "
            f"owner_key {owner_key!r} (run_id={lease.run_id!r}, pid={lease.pid}, "
            "verified still alive) — refusing a second concurrent owner. "
            "Pass force=True to explicitly take over, or investigate/stop "
            "the other holder first."
        )


@dataclass
class WorkerLease:
    """One registered external worker/MCP-server process.

    Mirrors the sprint notes' acceptance criteria: ``client``/``run_id``
    identity, ``pid`` + ``create_time`` + ``group_id``/``job_id`` for the
    PID-reuse guard (reusing ``process_lifecycle.OwnedProcessHandle``'s own
    fields — see :meth:`as_owned_handle`), and heartbeat/TTL bookkeeping.
    """

    run_id: str
    client: str
    pid: int
    executable: str = ""
    cwd: "str | None" = None
    cmdline: "list[str]" = field(default_factory=list)
    create_time: "float | None" = None
    group_id: "int | None" = None
    job_id: "int | None" = None
    shared_runtime: "str | None" = None
    # 39c8cf2c — logical single-owner identity (e.g. "tunnel-wrapper:<repo>"),
    # independent of PID/run_id. Two live leases sharing the same
    # (client, owner_key) is exactly the "old wrapper remained alive" bug;
    # see :meth:`ProcessLeaseBroker.acquire_exclusive`. Optional and additive
    # — leases that never set it (every pre-existing caller) are unaffected.
    owner_key: "str | None" = None
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    registered_at: float = 0.0
    last_heartbeat_at: float = 0.0
    released: bool = False
    released_at: "float | None" = None

    def to_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "WorkerLease":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def is_expired(self, now: float) -> bool:
        """True once *now* is past ``last_heartbeat_at + ttl_seconds`` AND
        the lease hasn't already been explicitly released — a released
        lease is done, not "expired"."""
        if self.released:
            return False
        return (now - self.last_heartbeat_at) > self.ttl_seconds

    def as_owned_handle(self) -> "_process_lifecycle.OwnedProcessHandle":
        """Adapt this lease to a ``process_lifecycle.OwnedProcessHandle`` so
        the existing PID-reuse guard (``verify_handle_live``) can be reused
        verbatim instead of re-implemented here."""
        return _process_lifecycle.OwnedProcessHandle(
            run_id=self.run_id,
            pid=self.pid,
            executable=self.executable,
            cwd=self.cwd,
            cmdline=list(self.cmdline),
            create_time=self.create_time,
            group_id=self.group_id,
            job_id=self.job_id,
        )


class ProcessLeaseBroker:
    """In-memory (optionally file-persisted) cross-client worker-lease
    registry. See module docstring for the full protocol contract."""

    def __init__(
        self,
        *,
        persist_path: "Path | None" = None,
        clock: "Any" = time.time,
        autosave: bool = True,
    ) -> None:
        self._leases: "dict[str, WorkerLease]" = {}
        # shared_runtime name -> set of (client, run_id) holders.
        self._shared_runtime_holders: "dict[str, set[tuple[str, str]]]" = {}
        self._clock = clock
        self._persist_path = persist_path
        self._autosave = autosave and persist_path is not None
        if self._persist_path is not None:
            self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        assert self._persist_path is not None
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return  # corrupt file — start from an empty registry rather than crash
        for row in data.get("leases", []) or []:
            try:
                lease = WorkerLease.from_dict(row)
            except Exception:  # noqa: BLE001 — one bad row must not break the whole load
                continue
            self._leases[lease.run_id] = lease
        for name, holders in (data.get("shared_runtime_holders") or {}).items():
            self._shared_runtime_holders[name] = {tuple(h) for h in holders}

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "leases": [lease.to_dict() for lease in self._leases.values()],
            "shared_runtime_holders": {
                name: [list(holder) for holder in holders]
                for name, holders in self._shared_runtime_holders.items()
            },
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._persist_path.parent), prefix=".process_leases_", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, self._persist_path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass

    def _maybe_save(self) -> None:
        if self._autosave:
            self._save()

    # -- registration ----------------------------------------------------

    def register(
        self,
        client: str,
        pid: int,
        *,
        run_id: "str | None" = None,
        executable: str = "",
        cwd: "str | None" = None,
        cmdline: "list[str] | None" = None,
        create_time: "float | None" = None,
        group_id: "int | None" = None,
        job_id: "int | None" = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        shared_runtime: "str | None" = None,
        owner_key: "str | None" = None,
    ) -> WorkerLease:
        """Register a new lease for *client*'s process *pid*. Generates a
        fresh ``run_id`` (via ``process_lifecycle.new_run_id`` — independent
        of, and stable across, PID reuse) unless the caller supplies one.
        Raises ``ValueError`` if the supplied ``run_id`` is already live —
        registration is not an upsert.

        *owner_key* (39c8cf2c) is an optional logical single-owner identity;
        see :meth:`acquire_exclusive` for the exclusivity semantics built on
        top of it. Passing it here directly (without going through
        :meth:`acquire_exclusive`) does NOT enforce exclusivity — it only
        tags the lease so a later ``acquire_exclusive`` call can find it."""
        if not client:
            raise ValueError("client is required")
        run_id = run_id or _process_lifecycle.new_run_id()
        existing = self._leases.get(run_id)
        if existing is not None and not existing.released:
            raise ValueError(f"run_id {run_id!r} is already registered")
        now = self._clock()
        lease = WorkerLease(
            run_id=run_id,
            client=client,
            pid=int(pid),
            executable=executable,
            cwd=cwd,
            cmdline=list(cmdline or []),
            create_time=create_time,
            group_id=group_id,
            job_id=job_id,
            shared_runtime=shared_runtime,
            owner_key=owner_key,
            ttl_seconds=float(ttl_seconds),
            registered_at=now,
            last_heartbeat_at=now,
        )
        self._leases[run_id] = lease
        if shared_runtime:
            self._shared_runtime_holders.setdefault(shared_runtime, set()).add((client, run_id))
        self._maybe_save()
        return lease

    def acquire_exclusive(
        self,
        client: str,
        owner_key: str,
        pid: int,
        *,
        executable: str = "",
        cwd: "str | None" = None,
        cmdline: "list[str] | None" = None,
        create_time: "float | None" = None,
        group_id: "int | None" = None,
        job_id: "int | None" = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        run_id: "str | None" = None,
        force: bool = False,
    ) -> WorkerLease:
        """39c8cf2c — single-owner lease acquisition with stale-wrapper
        detection/replacement, built on the existing PID-reuse-guarded
        liveness check (:meth:`is_process_alive`) rather than a new
        mechanism.

        Looks at every live (non-released) lease already registered by
        *client* under this *owner_key*:

        * None found → register and return a fresh lease. The common cold-
          start case.
        * Found, but :meth:`is_process_alive` says it's NOT the same live
          process any more (the recorded PID exited, or the OS reused the
          PID for something else — verified via create_time, never a bare
          ``pid_exists``) → this is a genuine STALE WRAPPER. It is
          automatically released (nothing to kill — it's already gone) and
          a fresh lease takes over. This is "stale-wrapper detection and
          replacement."
        * Found, and :meth:`is_process_alive` confirms it's still genuinely
          running → a real second owner would coexist with a live first
          owner, the exact incident this exists to prevent. Refuses with
          :class:`OwnerConflictError` UNLESS *force* is True, in which case
          the caller has explicitly asked to take over: the old lease is
          released (still never killing the underlying process — that
          remains the caller's own decision) and a fresh one is registered.

        Never kills a process itself, mirroring every other method on this
        broker.
        """
        for existing in self.list_leases(client=client):
            if existing.owner_key != owner_key:
                continue
            if self.is_process_alive(existing):
                if not force:
                    raise OwnerConflictError(client, owner_key, existing)
                self.release(client, existing.run_id)
            else:
                # Stale wrapper: verified NOT the same live process anymore.
                # Nothing to kill — just stop tracking it as live.
                self.release(client, existing.run_id)
        return self.register(
            client,
            pid,
            run_id=run_id,
            executable=executable,
            cwd=cwd,
            cmdline=cmdline,
            create_time=create_time,
            group_id=group_id,
            job_id=job_id,
            ttl_seconds=ttl_seconds,
            owner_key=owner_key,
        )

    def _get_owned(self, client: str, run_id: str) -> WorkerLease:
        lease = self._leases.get(run_id)
        if lease is None or lease.released:
            raise LeaseNotFoundError(run_id)
        if lease.client != client:
            raise ForeignLeaseError(
                f"client {client!r} does not own lease {run_id!r} (registered by {lease.client!r})"
            )
        return lease

    def heartbeat(self, client: str, run_id: str) -> WorkerLease:
        """Refresh *run_id*'s expiry. Raises :class:`LeaseNotFoundError` for
        an unknown/released run_id, :class:`ForeignLeaseError` if *client*
        didn't register it."""
        lease = self._get_owned(client, run_id)
        lease.last_heartbeat_at = self._clock()
        self._maybe_save()
        return lease

    def release(self, client: str, run_id: str) -> WorkerLease:
        """Explicit, graceful release on normal client stop. Same ownership
        guardrails as :meth:`heartbeat`."""
        lease = self._get_owned(client, run_id)
        lease.released = True
        lease.released_at = self._clock()
        if lease.shared_runtime:
            holders = self._shared_runtime_holders.get(lease.shared_runtime)
            if holders is not None:
                holders.discard((client, run_id))
        self._maybe_save()
        return lease

    def request_cleanup(self, requesting_client: str, run_id: str) -> WorkerLease:
        """Authorize a destructive cleanup of *run_id* — the ONE call
        intended to gate a caller's own kill of the underlying process.
        This method never kills anything itself; it only marks the lease
        released, and only for the client that registered it.
        ``ForeignLeaseError``/``LeaseNotFoundError`` here means "the broker
        refuses" — the sprint notes' "refuse unregistered destructive
        cleanup" / "no-cross-session-kill" guardrails, enforced identically
        to :meth:`release` (this IS a release, just named for the caller's
        actual intent)."""
        return self.release(requesting_client, run_id)

    def list_leases(self, *, client: "str | None" = None, include_released: bool = False) -> "list[WorkerLease]":
        out = [lease for lease in self._leases.values() if (include_released or not lease.released)]
        if client is not None:
            out = [lease for lease in out if lease.client == client]
        return sorted(out, key=lambda lease: lease.registered_at)

    def sweep_expired(self, *, now: "float | None" = None) -> "list[WorkerLease]":
        """Expiry recovery: return every live lease whose heartbeat has
        lapsed past its TTL. Read-only — this does NOT release, mutate, or
        kill anything; it is purely a reporting sweep a caller (e.g.
        :func:`report_unowned_survivors`, or a periodic dispatcher hook)
        builds on."""
        now = self._clock() if now is None else now
        return [lease for lease in self._leases.values() if lease.is_expired(now)]

    def is_process_alive(self, lease: WorkerLease) -> bool:
        """PID-reuse-guarded liveness check, delegating to
        ``process_lifecycle.verify_handle_live``."""
        return _process_lifecycle.verify_handle_live(lease.as_owned_handle())

    def report_unowned_survivors(self, *, now: "float | None" = None) -> "list[WorkerLease]":
        """Of the expired leases (:meth:`sweep_expired`), the subset whose
        OS process is STILL the SAME live process (verified create_time —
        never fooled by a PID the OS since reused for something else). This
        is the crashed-client-left-an-orphan case the sprint notes call
        out. Reported only, never killed — the broker's whole design
        refuses to guess-and-kill; a caller decides what to do with the
        report."""
        return [lease for lease in self.sweep_expired(now=now) if self.is_process_alive(lease)]

    # -- shared runtime reference counting -------------------------------

    def acquire_shared_runtime(self, name: str, client: str, run_id: str) -> int:
        """Record that (*client*, *run_id*) holds a reference to the shared
        runtime *name* (e.g. one Serena daemon several MCP clients share).
        Idempotent for the same holder. Returns the new holder count."""
        holders = self._shared_runtime_holders.setdefault(name, set())
        holders.add((client, run_id))
        self._maybe_save()
        return len(holders)

    def release_shared_runtime(self, name: str, client: str, run_id: str) -> int:
        """Drop (*client*, *run_id*)'s reference to shared runtime *name*.
        Returns the remaining holder count (0 means safe to actually tear
        the runtime down — see :meth:`should_close_shared_runtime`)."""
        holders = self._shared_runtime_holders.get(name)
        if holders is not None:
            holders.discard((client, run_id))
        self._maybe_save()
        return len(self._shared_runtime_holders.get(name, ()))

    def shared_runtime_refcount(self, name: str) -> int:
        return len(self._shared_runtime_holders.get(name, ()))

    def should_close_shared_runtime(self, name: str) -> bool:
        """True once every holder has released *name* — the signal for
        whichever caller actually owns the shared runtime's lifecycle that
        it's now safe (no more peers depend on it) to tear it down."""
        return self.shared_runtime_refcount(name) == 0


# ---------------------------------------------------------------------------
# c5c3fc5f — capture-aware async wrappers (agent/subprocess execution-event
# boundary)
# ---------------------------------------------------------------------------
#
# ProcessLeaseBroker.register/release above stay exactly as they were:
# synchronous, and with ZERO import of meridian.db/meridian.ai_log/
# meridian.session_tools — this module's own docstring's "lightweight,
# dependency-free, local-machine contract" is unchanged (the CLI wrapper at
# the bottom of this file, and every existing in-process caller such as
# tunnel_client.py, keep working unmodified against the sync methods).
#
# These two functions are an ADDITIVE, opt-in async layer on top: a caller
# that DOES have DB/project context (meridian.session_tools's
# capture_process_registered/capture_process_released, wired here via
# dependency injection rather than a direct import — see meridian.
# session_tools's own module docstring for the full boundary contract) can
# register a lease AND get a best-effort execution-event recorded in one
# call, without this module ever importing anything DB-shaped itself. A
# capture callback that raises is swallowed here: a broken/degraded
# execution-event sink must never undo, retry, or otherwise affect a lease
# that already registered/released synchronously above it — this is the
# "disabled/failed sinks do not lose the local event receipt" contract
# applied to THIS boundary specifically (the "local event receipt" for a
# lease IS the WorkerLease itself, already durably persisted by
# ProcessLeaseBroker._save() before `capture` is ever invoked).
ProcessCaptureFn = Callable[[WorkerLease], Awaitable[Any]]


async def register_process(
    broker: ProcessLeaseBroker,
    client: str,
    pid: int,
    *,
    run_id: "str | None" = None,
    executable: str = "",
    cwd: "str | None" = None,
    cmdline: "list[str] | None" = None,
    create_time: "float | None" = None,
    group_id: "int | None" = None,
    job_id: "int | None" = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    shared_runtime: "str | None" = None,
    owner_key: "str | None" = None,
    capture: "ProcessCaptureFn | None" = None,
) -> WorkerLease:
    """Register a lease exactly like :meth:`ProcessLeaseBroker.register`
    (synchronous call, unchanged), then — only if *capture* is supplied —
    best-effort ``await capture(lease)`` so the caller can record an
    ``agent.registered`` execution event. *capture* failing (or being
    omitted entirely) never affects the returned, already-registered
    ``WorkerLease``. Typical caller shape::

        from meridian import session_tools
        lease = await register_process(
            broker, "codex", pid,
            capture=lambda lease: session_tools.capture_process_registered(
                db, project_id=project_id, run_id=lease.run_id,
                client=lease.client, executable=lease.executable,
                cwd=lease.cwd, owner_key=lease.owner_key,
            ),
        )
    """
    lease = broker.register(
        client, pid,
        run_id=run_id, executable=executable, cwd=cwd, cmdline=cmdline,
        create_time=create_time, group_id=group_id, job_id=job_id,
        ttl_seconds=ttl_seconds, shared_runtime=shared_runtime,
        owner_key=owner_key,
    )
    if capture is not None:
        try:
            await capture(lease)
        except Exception:  # noqa: BLE001 — capture must never affect an already-registered lease
            pass
    return lease


async def release_process(
    broker: ProcessLeaseBroker,
    client: str,
    run_id: str,
    *,
    capture: "ProcessCaptureFn | None" = None,
) -> WorkerLease:
    """Release a lease exactly like :meth:`ProcessLeaseBroker.release`
    (synchronous call, unchanged, same ``LeaseNotFoundError``/
    ``ForeignLeaseError`` guardrails), then — only if *capture* is supplied
    — best-effort ``await capture(lease)`` so the caller can record an
    ``agent.released`` execution event. See :func:`register_process` for the
    full contract (identical shape, release half)."""
    lease = broker.release(client, run_id)
    if capture is not None:
        try:
            await capture(lease)
        except Exception:  # noqa: BLE001 — capture must never affect an already-released lease
            pass
    return lease


# ---------------------------------------------------------------------------
# Process-wide default broker
# ---------------------------------------------------------------------------

_default_broker: "ProcessLeaseBroker | None" = None


def get_broker() -> ProcessLeaseBroker:
    """Lazily-constructed, process-wide default broker, persisted to
    :func:`default_registry_path`. Matches the ``_owned_process_backend()``
    lazy-singleton pattern already used in ``tunnel_client.py`` for the
    portable lifecycle backend."""
    global _default_broker
    if _default_broker is None:
        _default_broker = ProcessLeaseBroker(persist_path=default_registry_path())
    return _default_broker


def reset_default_broker() -> None:
    """Test seam: drop the cached singleton so the next :func:`get_broker`
    call re-reads ``MERIDIAN_LEASE_REGISTRY_PATH`` (or the real home
    directory) from scratch."""
    global _default_broker
    _default_broker = None


# ---------------------------------------------------------------------------
# Client-neutral CLI / stdio wrapper
# ---------------------------------------------------------------------------
#
# Any external client (Claude Code, Codex, Claude Desktop, or a shell
# script) can speak this protocol without importing Python at all:
#
#   python -m meridian.process_registry register --client codex --pid 4242
#   python -m meridian.process_registry heartbeat --client codex --run-id <id>
#   python -m meridian.process_registry release   --client codex --run-id <id>
#   python -m meridian.process_registry list
#   python -m meridian.process_registry survivors
#
# Every subcommand prints one JSON object (or array) to stdout and exits 0
# on success, 1 on a protocol error (JSON {"error": "..."} on stderr) — the
# "documented hook contract" the sprint notes ask for, usable as a
# pre/post hook around any MCP-server spawn regardless of the spawning
# client's own language/runtime.


def _lease_json(lease: WorkerLease) -> "dict[str, Any]":
    return lease.to_dict()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.process_registry",
        description="Client-neutral worker-lease broker CLI (315b0a63).",
    )
    parser.add_argument(
        "--registry-path", default=None,
        help="Override the persisted registry file (defaults to MERIDIAN_LEASE_REGISTRY_PATH or ~/.meridian/process_leases.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register", help="Register a new worker lease.")
    reg.add_argument("--client", required=True)
    reg.add_argument("--pid", required=True, type=int)
    reg.add_argument("--run-id", default=None)
    reg.add_argument("--executable", default="")
    reg.add_argument("--cwd", default=None)
    reg.add_argument("--cmdline", default=None, help="JSON array of argv strings.")
    reg.add_argument("--create-time", default=None, type=float)
    reg.add_argument("--ttl-seconds", default=DEFAULT_TTL_SECONDS, type=float)
    reg.add_argument("--shared-runtime", default=None)

    hb = sub.add_parser("heartbeat", help="Refresh a lease's expiry.")
    hb.add_argument("--client", required=True)
    hb.add_argument("--run-id", required=True)

    rel = sub.add_parser("release", help="Explicitly release a lease.")
    rel.add_argument("--client", required=True)
    rel.add_argument("--run-id", required=True)

    lst = sub.add_parser("list", help="List live leases.")
    lst.add_argument("--client", default=None)
    lst.add_argument("--include-released", action="store_true")

    sub.add_parser("survivors", help="Report expired-but-still-alive leases.")

    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    persist_path = Path(args.registry_path) if args.registry_path else default_registry_path()
    broker = ProcessLeaseBroker(persist_path=persist_path)

    try:
        if args.command == "register":
            cmdline = json.loads(args.cmdline) if args.cmdline else []
            lease = broker.register(
                args.client,
                args.pid,
                run_id=args.run_id,
                executable=args.executable,
                cwd=args.cwd,
                cmdline=cmdline,
                create_time=args.create_time,
                ttl_seconds=args.ttl_seconds,
                shared_runtime=args.shared_runtime,
            )
            print(json.dumps(_lease_json(lease)))
        elif args.command == "heartbeat":
            lease = broker.heartbeat(args.client, args.run_id)
            print(json.dumps(_lease_json(lease)))
        elif args.command == "release":
            lease = broker.release(args.client, args.run_id)
            print(json.dumps(_lease_json(lease)))
        elif args.command == "list":
            leases = broker.list_leases(client=args.client, include_released=args.include_released)
            print(json.dumps([_lease_json(lease) for lease in leases]))
        elif args.command == "survivors":
            leases = broker.report_unowned_survivors()
            print(json.dumps([_lease_json(lease) for lease in leases]))
        else:  # pragma: no cover — argparse `required=True` prevents this
            parser.error(f"unknown command {args.command!r}")
            return 2
    except (LeaseNotFoundError, ForeignLeaseError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via main() in tests
    sys.exit(main())
